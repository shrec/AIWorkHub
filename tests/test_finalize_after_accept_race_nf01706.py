"""NF-2026-01706: a late supervisor finalization never follows an accepted row.

Measured on reviewer request 0f2b0fad9e404a9791c53732dd3f3f67: review_ready ->
accepted -> finalizing (card not archived yet) -> release_pending -> finalizing
(card archived) -> finalize_failed. The NF-2026-01625 guard missed both times:
once because the card was not archived yet, once because the latest row was
``release_pending`` rather than ``accepted``. The card's acceptance seal and
receipt for this request and claim epoch are what make an attempt final.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Callable

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from test_reviewer_accept_before_finalize_nf01625 import (  # noqa: E402
    CLAIM_EPOCH,
    REQUEST_ID,
    TASK_ID,
    _accept,
    _archive,
    _automatic_review,
    _ledger,
    _review_ready_reviewer,
    _states,
    _superseded_after_acceptance,
    _usage_records,
)

from aiworkhub import process_launcher, task_store, worker_workspace  # noqa: E402


def _identity(case) -> dict:
    row = case.accepted_row
    return {key: row[key] for key in ("request_id", "task_id", "runner", "topic", "adapter_id")}


def test_finalization_of_an_accepted_attempt_not_yet_archived_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _accept(case)
    card = task_store.get_task(case.repo, TASK_ID)
    assert card is not None and task_store.canonical_status(card) != "archived"

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert _states(case.manager) == ["running", "review_ready", "accepted"]
    assert _usage_records(case.repo) == 1


def test_finalization_after_accepted_then_release_pending_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _automatic_review(case)
    _ledger(case.manager, {**_identity(case), "state": "finalizing"})
    _ledger(
        case.manager,
        {**_identity(case), "state": "release_pending", "error": "worktree_is_not_detached"},
    )
    before = _states(case.manager)
    assert before[-1] == "release_pending" and "accepted" in before

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert _states(case.manager) == before
    assert "finalize_failed" not in _states(case.manager)


def _no_accepted_row(case) -> None:
    pass


@pytest.mark.parametrize(
    ("dispose", "claim_epoch", "request_id"),
    [
        pytest.param(_accept, CLAIM_EPOCH + 1, REQUEST_ID, id="another_claim_epoch"),
        pytest.param(_accept, CLAIM_EPOCH, "f" * 32, id="another_request"),
        pytest.param(_superseded_after_acceptance, CLAIM_EPOCH, REQUEST_ID, id="superseded"),
        pytest.param(_no_accepted_row, CLAIM_EPOCH, REQUEST_ID, id="no_seal"),
    ],
)
def test_guard_does_not_fire_without_this_attempts_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispose: Callable[[object], object],
    claim_epoch: int,
    request_id: str,
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    dispose(case)
    events = case.manager._request_events(REQUEST_ID)
    if not any(row.get("state") == "accepted" for row in events):
        events = [*events, {**_identity(case), "state": "accepted", "accepted": True}]
    metadata = {"task_id": TASK_ID, "claim_epoch": claim_epoch}

    assert case.manager._accepted_attempt_row(events, metadata, request_id) is None
    card = task_store.get_task(case.repo, TASK_ID)
    assert card is not None
    assert not task_store.accepted_this_attempt(
        card, request_id=request_id, claim_epoch=claim_epoch
    )


def test_guard_needs_an_accepted_row_and_returns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _accept(case)
    events = case.manager._request_events(REQUEST_ID)
    metadata = {"task_id": TASK_ID, "claim_epoch": CLAIM_EPOCH}
    without_accepted = [row for row in events if row.get("state") != "accepted"]
    unaccepted = [
        {**row, "accepted": False} if row.get("state") == "accepted" else row for row in events
    ]

    assert case.manager._accepted_attempt_row(without_accepted, metadata, REQUEST_ID) is None
    assert case.manager._accepted_attempt_row(unaccepted, metadata, REQUEST_ID) is None
    found = case.manager._accepted_attempt_row(events, metadata, REQUEST_ID)
    assert found is not None and found["state"] == "accepted"


def test_archived_after_accepting_still_requires_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _accept(case)
    card = task_store.get_task(case.repo, TASK_ID)
    assert card is not None
    kwargs = {"request_id": REQUEST_ID, "claim_epoch": CLAIM_EPOCH}
    assert task_store.accepted_this_attempt(card, **kwargs) is True
    assert task_store.archived_after_accepting(card, **kwargs) is False

    _archive(case, actor=case.accepted_row["runner"], reason="automatic review accepted")
    card = task_store.get_task(case.repo, TASK_ID)
    assert card is not None
    assert task_store.archived_after_accepting(card, **kwargs) is True
    assert task_store.accepted_this_attempt(card, **kwargs) is True


# NF-2026-02186: 22 reviewers and 1 worker ended finalize_failed after their
# ledger already held state=accepted. The rows below are the measured sequence.
DEFERRED = "accepted_card_unreadable"
CLAIM_LOST = "claim_ownership_lost:state=archived"


def _finalizing_review_ready_accepted(case) -> list[str]:
    """The measured ledger: finalizing, review_ready, accepted on a sealed card."""
    _ledger(case.manager, {**_identity(case), "state": "finalizing"})
    path = case.manager.process_log_path
    *head, review_ready, finalizing = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([*head, finalizing, review_ready, ""]), encoding="utf-8")
    _accept(case)
    states = _states(case.manager)
    assert states[-3:] == ["finalizing", "review_ready", "accepted"]
    return states


def _claim_lost_after_scope(
    case, monkeypatch: pytest.MonkeyPatch, before_raise: Callable[[], object] = lambda: None
) -> None:
    """The scope check passes and the claim read reports the card as archived."""

    def lost(_metadata: object) -> str:
        before_raise()
        raise worker_workspace.WorkspaceError(CLAIM_LOST)

    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(case.manager, "_exact_claim_state", lost)


def _tail_state(case) -> str:
    rows = case.manager._request_events(REQUEST_ID)
    return str(process_launcher.split_lifecycle_tail(rows)[0].get("state") or "")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(process_launcher.LaunchRejected("card_unreadable"), id="launch_rejected"),
        pytest.param(sqlite3.OperationalError("database is locked"), id="store_error"),
    ],
)
def test_unreadable_card_at_the_gate_defers_and_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    before = _finalizing_review_ready_accepted(case)

    def unreadable(_task_id: str) -> str:
        raise failure

    monkeypatch.setattr(case.manager, "_show_task", unreadable)

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["reconciliation_deferred"] == DEFERRED, event
    assert event["state"] == "accepted"
    assert _states(case.manager) == before


def test_first_gate_read_failing_cannot_fail_an_accepted_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _automatic_review(case)
    readable = case.manager._show_task
    reads: list[str] = []

    def first_read_fails(task_id: str) -> object:
        reads.append(task_id)
        if len(reads) == 1:
            raise process_launcher.LaunchRejected("card_unreadable")
        return readable(task_id)

    monkeypatch.setattr(case.manager, "_show_task", first_read_fails)
    _claim_lost_after_scope(case, monkeypatch)

    case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert "finalize_failed" not in _states(case.manager)
    assert _tail_state(case) == "accepted"

    # The card is readable again: the next pass settles on the same row.
    before = _states(case.manager)
    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)
    assert event is not None and event["state"] == "accepted", event
    assert _states(case.manager) == before


def test_sealed_row_is_restored_over_a_finalizing_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _accept(case)
    _ledger(case.manager, {**_identity(case), "state": "finalizing"})
    before = _states(case.manager)

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert event["restored_over_state"] == "finalizing"
    assert event["quality_review_receipt"] == case.sealed
    assert _states(case.manager) == [*before, "accepted"]


def test_finalizing_tail_with_a_differing_receipt_is_not_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _accept(case)
    _ledger(
        case.manager,
        {
            **case.accepted_row,
            "workspace_disposition": "removed",
            "quality_review_receipt": {**case.sealed, "differs": True},
        },
    )
    _ledger(case.manager, {**_identity(case), "state": "finalizing"})
    before = _states(case.manager)

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert "restored_over_state" not in event
    assert _states(case.manager) == before


def test_ownership_loss_restores_the_sealed_row_over_finalizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    _automatic_review(case)
    real_gate = case.manager._accepted_attempt_row
    gates: list[str] = []

    def gate_misses_once(events: list, metadata: dict, request_id: str) -> object:
        gates.append(request_id)
        return None if len(gates) == 1 else real_gate(events, metadata, request_id)

    monkeypatch.setattr(case.manager, "_accepted_attempt_row", gate_misses_once)
    _claim_lost_after_scope(case, monkeypatch)

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert event["restored_over_state"] == "finalizing"
    assert _states(case.manager)[-2:] == ["finalizing", "accepted"]
    assert "finalize_failed" not in _states(case.manager)


def test_acceptance_landing_during_the_pass_is_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    # ``review_ready`` is terminal: without this row the pass returns at its first guard.
    _ledger(case.manager, {**_identity(case), "state": "finalizing"})
    _claim_lost_after_scope(case, monkeypatch, before_raise=lambda: _accept(case))

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["state"] == "accepted", event
    assert "finalize_failed" not in _states(case.manager)
    assert _tail_state(case) == "accepted"


def test_ownership_loss_with_an_unreadable_card_defers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _review_ready_reviewer(tmp_path, monkeypatch)
    # ``review_ready`` is terminal: without this row the pass returns at its first guard.
    _ledger(case.manager, {**_identity(case), "state": "finalizing"})

    def unreadable(_task_id: str) -> str:
        raise process_launcher.LaunchRejected("card_unreadable")

    def accepted_then_unreadable() -> None:
        _accept(case)
        monkeypatch.setattr(case.manager, "_show_task", unreadable)

    _claim_lost_after_scope(case, monkeypatch, before_raise=accepted_then_unreadable)

    event = case.manager._finalize_after_process_exit(REQUEST_ID, 0)

    assert event is not None and event["reconciliation_deferred"] == DEFERRED, event
    assert "finalize_failed" not in _states(case.manager)
    assert _tail_state(case) == "accepted"
