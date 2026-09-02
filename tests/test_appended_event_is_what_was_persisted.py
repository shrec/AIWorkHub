"""One event, one representation: what is returned is what was written.

``append_event`` canonicalises a failure-terminal row on the way in -- it
builds ``terminal_reason`` from server-side constants rather than trusting the
caller. It did that on a private copy and returned nothing, so the launcher
handed its own pre-canonical dict back to whoever finalized a request, while
the durable ledger held a different, richer row.

Nothing detected the split until a finalization was replayed: the first call
returned the caller's dict, the replay read the ledger, and the same terminal
event compared unequal to itself. Three reconciler tests stood red on exactly
that -- a live worker's finalization and its own audit record disagreeing about
why the worker died.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as pel  # noqa: E402


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_failure_terminal_event_returns_exactly_the_persisted_row(tmp_path: Path):
    log = tmp_path / "process_events.jsonl"
    returned = pel.append_event(
        log,
        {
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "request_id": "r1",
            "state": "worker_failed",
            "error": "supervisor_incomplete:state=missing:rc=None",
        },
    )
    (persisted,) = _rows(log)
    assert returned == persisted
    # and the canonicalisation really did happen -- this is not a vacuous pass
    assert isinstance(returned["terminal_reason"], dict)
    assert returned["terminal_reason"]["code"] == "worker_failed"


def test_the_caller_dict_is_never_mutated(tmp_path: Path):
    """The ledger owns canonicalisation; the caller's object stays its own."""
    log = tmp_path / "process_events.jsonl"
    event = {"request_id": "r2", "state": "worker_failed", "error": "boom"}
    returned = pel.append_event(log, event)
    assert "terminal_reason" not in event
    assert returned["terminal_reason"]["message"] == "boom"


def test_a_non_terminal_event_round_trips_unchanged(tmp_path: Path):
    log = tmp_path / "process_events.jsonl"
    event = {"request_id": "r3", "state": "running", "pid": 42}
    returned = pel.append_event(log, event)
    (persisted,) = _rows(log)
    assert returned == persisted == event


def test_a_replayed_terminal_event_compares_equal_to_the_original(tmp_path: Path):
    """The exact shape the three reconciler tests were asserting."""
    log = tmp_path / "process_events.jsonl"
    first = pel.append_event(
        log, {"request_id": "r4", "state": "cancelled", "error": "worker_cancelled"}
    )
    replayed = pel.latest_events(log, key_field="request_id")["r4"]
    assert replayed == first
