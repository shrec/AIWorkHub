from __future__ import annotations

import sqlite3

import pytest

from aiworkhub import sdlc_outcome_metrics, task_engine

_REPO = "repo-one"


def _receipt(task_id: str, request_id: str, claim_epoch: int = 1):
    digest = "a" * 64
    hashes = {"fixed.txt": digest}
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "base_oid": "base",
        "promoted_paths": ["fixed.txt"],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": digest,
        "repository_revision": "sha256:" + task_engine._canonical_json_hash({
            "base_oid": "base", "changed_path_hashes": hashes
        }),
    }
    return {**unsigned, "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned)}


class _Events:
    def __init__(self):
        self.rows: list[dict] = []

    def history(self, task_id: str, *names: str) -> None:
        request_id = f"R-{task_id}"
        for name in ("claim_start", *names, "review_ready"):
            self.add(task_id, name)
        self.add(
            task_id,
            "accept_review",
            {"request_id": request_id, "accepted_outcome_receipt": _receipt(task_id, request_id)},
        )

    def add(self, task_id: str, event: str, payload: dict | None = None) -> None:
        self.rows.append({
            "event_id": len(self.rows) + 1, "task_id": task_id, "event": event,
            "payload": payload or {},
        })

    def aggregate(self, cards=None):
        return sdlc_outcome_metrics.aggregate(
            self.rows, [], repository_id=_REPO, task_cards=cards
        )


def _first_pass(result):
    return result["first_pass_acceptance"]["numerator"], result["first_pass_acceptance"][
        "denominator"
    ]


def test_reject_rework_accept_and_rework_successor_are_not_first_pass():
    events = _Events()
    events.history("FIRST")
    events.history("REJECTED_THEN_ACCEPTED", "reject_review", "claim_start")
    events.history("SUCCESSOR")
    result = events.aggregate({
        "FIRST": {}, "REJECTED_THEN_ACCEPTED": {},
        "SUCCESSOR": {"rework_predecessor": {"task_id": "REJECTED_THEN_ACCEPTED"}},
    })

    assert _first_pass(result) == (1, 3)
    assert result["first_pass_acceptance_basis"]["rework_successors"] == 1
    assert result["first_pass_acceptance_basis"]["rework_predecessor_evidence"] == "read"
    assert result["review_rounds_per_accepted_task"]["numerator"] == 4


@pytest.mark.parametrize(
    "event", ["blocked_rework_recovery", "retry_terminal", "transient_retry", "superseded"]
)
def test_rework_events_without_a_reject_row_are_not_first_pass(event):
    events = _Events()
    events.history("CLEAN")
    events.history("REWORKED", event)
    result = events.aggregate({"CLEAN": {}, "REWORKED": {}})

    assert _first_pass(result) == (1, 2)
    assert result["review_rounds_per_accepted_task"]["numerator"] == 2


def test_rework_after_the_first_accept_does_not_change_first_pass():
    events = _Events()
    events.history("LATE", )
    events.add("LATE", "blocked_rework_recovery")

    assert _first_pass(events.aggregate({"LATE": {}})) == (1, 1)


def test_unread_card_store_is_reported_unknown_not_assumed_clean():
    events = _Events()
    events.history("ONLY")
    result = events.aggregate(None)

    assert _first_pass(result) == (1, 1)
    assert result["first_pass_acceptance_basis"]["rework_predecessor_evidence"] == "UNKNOWN"
    assert result["first_pass_acceptance_basis"]["unknown_reason"] == "not_supplied"


def test_read_task_cards_marks_cards_naming_a_rework_predecessor(tmp_path):
    path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, card_json TEXT)")
    for task_id, card in (
        ("PLAIN", '{"topic": "t", "rework_predecessor": null}'),
        ("SUCCESSOR", '{"topic": "t", "rework_predecessor": {"task_id": "PLAIN"}}'),
    ):
        conn.execute("INSERT INTO tasks VALUES (?, ?)", (task_id, card))
    conn.commit()
    found = sdlc_outcome_metrics.read_task_cards(conn, ["PLAIN", "SUCCESSOR"])
    conn.close()

    assert found.available and found.rework_successors == frozenset({"SUCCESSOR"})
