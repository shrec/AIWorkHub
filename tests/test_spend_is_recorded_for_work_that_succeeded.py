"""Provider spend must survive the transition that proves the work succeeded.

``append_live_usage_event`` refused any card whose status was not
``processing``. The finalizer moves a successful worker to ``review`` and THEN
records its usage, so every worker that succeeded had its cost measured and
then thrown away with ``lifecycle_mismatch:review``. Only failures were cheap
enough to still be `processing` when the write landed.

The ledger therefore recorded losses and called them the whole picture. Measured
on this repository: request 92279514599a47e2a6c9dcdaf86f53fb observed
cost_usd 4.735654, 3,767,856 input and 55,822 output tokens, model observed --
and recorded none of it. Across the source_graph topic every by-model and
by-provider dimension read "unknown" with usage_observed_records = 0, while
retry economics showed a 57.1% retry rate and zero accepted retries: exactly
what a ledger looks like when success is invisible to it.

Identity is what prevents forgery here, and it is untouched: launch_request_id
and claim_epoch name one exact attempt, a re-claim increments the epoch, and
the note uniqueness makes the write idempotent. The card's current status
prevents nothing that identity does not already prevent.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_fsm, task_store  # noqa: E402

REQUEST_ID = "spend0000000000000000000000000ab"
TASK_ID = "TASK_SPEND_TRUTH"
RUNNER = "claude_worker"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    task_store.initialize_repository(root)
    return root


def _card(status: str) -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "coding",
        "status": status,
        "worker_status": "claimed" if status == "processing" else status,
        "claimed_by": RUNNER,
        "launch_request_id": REQUEST_ID,
        "claim_epoch": 1,
        "allowed_writes": [],
    }


def _seed(root: Path, status: str) -> None:
    db = task_store.canonical_db_path(root)
    card = _card(status)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tasks"
            "(task_id, runner, topic, status, worker_status, claimed_by, card_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (TASK_ID, RUNNER, "coding", status, card["worker_status"], RUNNER,
             json.dumps(card), "2026-09-02T00:00:00+00:00", "2026-09-02T00:00:00+00:00"),
        )


PAYLOAD = {
    "input_tokens": 3_767_856,
    "output_tokens": 55_822,
    "total_tokens": 3_823_678,
    "cost_usd": 4.735654,
    "cost_observed": True,
    "usage_observed": True,
    "role": "worker",
}


@pytest.mark.parametrize("status", sorted(task_fsm.CLAIM_ATTEMPT_ACCOUNTABLE_STATUSES))
def test_spend_records_in_every_state_one_claim_can_reach(tmp_path: Path, status: str):
    root = _repo(tmp_path)
    _seed(root, status)
    ok, reason = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id=REQUEST_ID, claimed_by=RUNNER, claim_epoch=1, payload=PAYLOAD,
    )
    assert ok, f"{status}: {reason}"


def test_review_is_the_state_that_used_to_lose_the_money(tmp_path: Path):
    """The specific regression, named."""
    root = _repo(tmp_path)
    _seed(root, "review")
    ok, reason = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id=REQUEST_ID, claimed_by=RUNNER, claim_epoch=1, payload=PAYLOAD,
    )
    assert ok and reason != "lifecycle_mismatch:review"

    db = task_store.canonical_db_path(root)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event='usage_record'",
            (TASK_ID,),
        ).fetchone()
    stored = json.loads(row["payload_json"])
    assert stored["cost_usd"] == pytest.approx(4.735654)
    assert stored["request_id"] == REQUEST_ID
    assert stored["claim_epoch"] == 1


def test_a_closed_record_still_refuses(tmp_path: Path):
    """Widening the gate is not removing it."""
    root = _repo(tmp_path)
    _seed(root, "archived")
    ok, reason = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id=REQUEST_ID, claimed_by=RUNNER, claim_epoch=1, payload=PAYLOAD,
    )
    assert not ok
    assert reason.startswith("lifecycle_mismatch")


def test_a_different_claim_epoch_still_refuses(tmp_path: Path):
    """Identity, not status, is what keeps a stale attempt out."""
    root = _repo(tmp_path)
    _seed(root, "review")
    ok, reason = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id=REQUEST_ID, claimed_by=RUNNER, claim_epoch=2, payload=PAYLOAD,
    )
    assert not ok
    assert reason == "claim_epoch_mismatch"


def test_a_foreign_request_id_still_refuses(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root, "review")
    ok, reason = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id="f" * 32, claimed_by=RUNNER, claim_epoch=1, payload=PAYLOAD,
    )
    assert not ok
    assert reason == "request_id_mismatch"


def test_recording_twice_counts_once(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root, "review")
    first = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id=REQUEST_ID, claimed_by=RUNNER, claim_epoch=1, payload=PAYLOAD,
    )
    second = task_store.append_live_usage_event(
        root, TASK_ID, RUNNER,
        request_id=REQUEST_ID, claimed_by=RUNNER, claim_epoch=1, payload=PAYLOAD,
    )
    assert first[0] and second[0]
    assert second[1] == "already_recorded"
    db = task_store.canonical_db_path(root)
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND event='usage_record'",
            (TASK_ID,),
        ).fetchone()[0]
    assert count == 1
