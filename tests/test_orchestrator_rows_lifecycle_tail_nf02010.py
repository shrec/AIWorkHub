"""Tests for NF-2026-02010: stateless review-orchestrator rows must never
become the lifecycle tail.

``review_orchestrator`` appends ``review_orchestrator_wait`` and
``review_orchestrator_mechanical_rework`` rows to a request's event history.
Neither carries a top-level ``state``. ``launch_zero_delta.split_lifecycle_tail``
used to return such a row as the lifecycle tail, so a ``review_ready``
candidate read as state ``None`` and was re-finalized in a loop. These tests
pin that the helper skips both row types (``ORCHESTRATOR_ADVISORY_EVENT_TYPES``)
and that a lifecycle reader therefore keeps the ``review_ready`` row.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import test_runtime_notice_lifecycle_readers_nf01194 as nf01194  # noqa: E402

from aiworkhub import launch_zero_delta, process_launcher  # noqa: E402

WAIT = "review_orchestrator_wait"
REWORK = "review_orchestrator_mechanical_rework"


def _orchestrator_row(event_type: str, request_id: str = "r") -> dict:
    return {
        "request_id": request_id,
        "task_id": nf01194.TASK_ID,
        "event_type": event_type,
        "reason": "review_orchestrator_deferred",
    }


def _notice_row() -> dict:
    return {"request_id": "r", "event_kind": launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND}


def test_trailing_wait_row_is_not_the_lifecycle_tail():
    review_ready = {"request_id": "r", "state": "review_ready"}
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [review_ready, _orchestrator_row(WAIT)]
    )
    assert lifecycle is review_ready
    assert advisory is None


def test_trailing_mechanical_rework_row_is_not_the_lifecycle_tail():
    review_ready = {"request_id": "r", "state": "review_ready"}
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [review_ready, _orchestrator_row(REWORK)]
    )
    assert lifecycle is review_ready
    assert advisory is None


def test_orchestrator_rows_and_notice_keep_notice_as_advisory():
    review_ready = {"request_id": "r", "state": "review_ready"}
    notice = _notice_row()
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [review_ready, notice, _orchestrator_row(WAIT), _orchestrator_row(REWORK)]
    )
    assert lifecycle is review_ready
    assert advisory is notice


def test_orchestrator_only_rows_return_the_empty_contract():
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [_orchestrator_row(WAIT), _orchestrator_row(REWORK)]
    )
    assert lifecycle == {}
    assert advisory is None


def test_unrelated_event_type_row_still_becomes_the_tail():
    review_ready = {"request_id": "r", "state": "review_ready"}
    other = {"request_id": "r", "event_type": "some_other_event", "state": "exited"}
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [review_ready, other, _orchestrator_row(WAIT)]
    )
    assert lifecycle is other
    assert advisory is None


def test_status_keeps_review_ready_and_does_not_refinalize_after_wait_row(
    tmp_path, monkeypatch,
):
    card = nf01194._card()
    manager = nf01194._manager(tmp_path, card)
    nf01194._seed_lifecycle(
        manager, tmp_path, card, pid=999_000, ticks=4242, state="review_ready",
    )
    manager._append_event(_orchestrator_row(WAIT, request_id=nf01194.REQUEST_ID))
    log_path = tmp_path / "events.jsonl"
    rows_before = log_path.read_text(encoding="utf-8").splitlines()

    finalize_calls: list = []
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_a, **_k: False)
    monkeypatch.setattr(
        manager,
        "_finalize_after_process_exit",
        lambda *a, **k: finalize_calls.append((a, k)),
    )

    result = manager.status(nf01194.REQUEST_ID)

    assert result["latest_event"].get("state") == "review_ready"
    assert result["latest_event"].get("event_type") != WAIT
    assert result["state"] == "review_ready"
    assert finalize_calls == []
    rows_after = log_path.read_text(encoding="utf-8").splitlines()
    assert rows_after == rows_before
    assert json.loads(rows_after[-1]).get("event_type") == WAIT
