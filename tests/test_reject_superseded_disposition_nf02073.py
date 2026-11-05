"""NF-2026-02073: a terminal reject reports the request it judged, not an older one.

Observed on 2026-11-05: a card rejected once to pending (request A, failure
category cancellation_or_timeout) was relaunched as request B, returned to
review_ready, and then rejected to superseded with a new reason and lesson.
The archive committed, but the receipt and the learning commit named request A
with the old reason identity and the infrastructure category.

Every test here drives ``core.reject_review`` for the real rejections.  The one
step that is not a production path is the relaunch of request B: no worker runs
in the test sandbox, so the card is returned to review_ready for request B
directly in the canonical row.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, learning_commit_store, server, task_store  # noqa: E402

NOW = "2026-11-05T00:00:00+00:00"
TASK = "T_TERMINAL_STALE_NF02073"
REQ_A = "req-a-cancelled-0001"
REQ_B = "req-b-review-0002"
FIRST_REASON = "first rework: the run was cancelled by the timeout"
NEW_REASON = "request B regressed the measured latency; do not rework it"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _db(root: Path) -> Path:
    return task_store.storage_readiness(root).canonical_db


def _insert(root: Path, task_id: str, card: dict) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", "coding", "solo", "review", "review", "normal",
             "obj", json.dumps(card), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _row(root: Path, task_id: str) -> dict:
    conn = sqlite3.connect(_db(root))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def _review_ready(request_id: str, *, with_manifest: bool) -> dict:
    evidence: dict = {
        "request_identity": {
            "request_id": request_id,
            "task_id": TASK,
            "runner": "claude_coding",
            "topic": "coding",
        },
    }
    if with_manifest:
        evidence["attempt_artifact_manifest"] = {
            "schema_id": "aiworkhub.attempt_artifact_manifest.v1",
        }
    return {"substatus": "review_ready", "request_id": request_id, "evidence": evidence}


def _relaunch_to_review_ready(root: Path, request_id: str) -> None:
    """Return the card to review_ready for a relaunched request (test stand-in)."""
    card = json.loads(_row(root, TASK)["card_json"])
    card["terminal_review"] = _review_ready(request_id, with_manifest=True)
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', "
            "claimed_by='claude_coding', card_json=? WHERE task_id=?",
            (json.dumps(card), TASK),
        )
        conn.commit()
    finally:
        conn.close()


def _reach_second_review(root: Path) -> None:
    """Reject request A to pending (real path), then relaunch it as request B."""
    first = core.reject_review(
        TASK, FIRST_REASON, to="pending", failure_category="cancellation_or_timeout",
    )
    assert first["ok"] is True, first
    pin = json.loads(_row(root, TASK)["card_json"])["rejection_disposition"]
    assert pin["request_id"] == REQ_A
    _relaunch_to_review_ready(root, REQ_B)


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    _insert(root, TASK, {"terminal_review": _review_ready(REQ_A, with_manifest=False)})
    return root


@pytest.mark.parametrize("to", ["superseded", "archived"])
def test_terminal_archive_reports_the_judged_request_and_new_reason(coord, to):
    _reach_second_review(coord)

    res = core.reject_review(TASK, NEW_REASON, to=to)

    assert res["ok"] is True, res
    card = json.loads(res["stdout"])
    pin = card["rejection_disposition"]
    assert pin["request_id"] == REQ_B
    assert pin["reason_identity"] == {
        "bytes": len(NEW_REASON.encode("utf-8")),
        "sha256": _sha(NEW_REASON),
        "truncated": False,
    }
    # The prior infrastructure category belonged to request A and must not carry over.
    assert pin["failure_category"] == ""

    receipt = server._reject_review_receipt_fields(card, task_id=TASK, reason=NEW_REASON, to=to)
    assert receipt["request_id"] == REQ_B
    assert receipt["reason_identity"]["sha256"] == _sha(NEW_REASON)
    assert not receipt["failure_category"]
    assert receipt["claim_epoch"] is None
    assert receipt["predecessor_changed_paths_count"] == 0

    # The learning commit binds to B only: the lesson cannot land on A.
    stored = json.loads(_row(coord, TASK)["card_json"])
    assert learning_commit_store._landed_manager_rejection(stored, REQ_B) is True
    assert learning_commit_store._landed_manager_rejection(stored, REQ_A) is False


def test_terminal_block_pins_the_judged_request_and_new_reason(coord):
    _reach_second_review(coord)

    res = core.reject_review(TASK, NEW_REASON, to="blocked")

    assert res["ok"] is True, res
    card = json.loads(res["stdout"])
    assert card["rejection_disposition"]["request_id"] == REQ_B
    assert card["rejection_disposition"]["reason_identity"]["sha256"] == _sha(NEW_REASON)
    receipt = server._reject_review_receipt_fields(card, task_id=TASK, reason=NEW_REASON, to="blocked")
    assert receipt["request_id"] == REQ_B
    assert receipt["reason_identity"]["sha256"] == _sha(NEW_REASON)


def test_terminal_reject_refuses_a_stale_explicit_predecessor(coord):
    _reach_second_review(coord)

    res = core.reject_review(TASK, NEW_REASON, to="superseded", predecessor_request_id=REQ_A)

    assert res["ok"] is False, res
    assert _row(coord, TASK)["worker_status"] == "review"


def test_first_time_terminal_reject_reports_what_an_unpinned_card_did(coord):
    # No prior rework episode: the pin carries no category.  The receipt must be
    # what this card reported before the pin existed, i.e. the same fields
    # computed with the pin removed.
    res = core.reject_review(TASK, NEW_REASON, to="archived")

    assert res["ok"] is True, res
    card = json.loads(res["stdout"])
    assert card["rejection_disposition"]["failure_category"] == ""
    unpinned = {key: value for key, value in card.items() if key != "rejection_disposition"}
    expected = server._reject_review_receipt_fields(unpinned, task_id=TASK, reason=NEW_REASON, to="archived")
    receipt = server._reject_review_receipt_fields(card, task_id=TASK, reason=NEW_REASON, to="archived")
    assert receipt["failure_category"] == expected["failure_category"]
    assert receipt["failure_category_source"] == expected["failure_category_source"]


def test_archive_card_updates_commit_with_the_archive(coord):
    pin = {
        "schema_id": "aiworkhub.rejection_disposition.v1",
        "request_id": REQ_B,
        "failure_category": "",
    }

    ok, state = task_store.archive_task(
        coord, TASK, operation="superseded", reason="new", card_updates={"rejection_disposition": pin},
    )

    assert (ok, state) == (True, "superseded")
    row = _row(coord, TASK)
    assert row["archived_at"]
    assert json.loads(row["card_json"])["rejection_disposition"] == pin


def test_refused_archive_stores_no_card_updates(coord):
    first = {"schema_id": "aiworkhub.rejection_disposition.v1", "request_id": REQ_B}
    assert task_store.archive_task(
        coord, TASK, operation="superseded", reason="new", card_updates={"rejection_disposition": first},
    ) == (True, "superseded")

    ok, _state = task_store.archive_task(
        coord, TASK, operation="archived", reason="again",
        card_updates={"rejection_disposition": {"request_id": REQ_A}},
    )

    assert ok is True  # already archived: the second archive is a no-op
    assert json.loads(_row(coord, TASK)["card_json"])["rejection_disposition"] == first


@pytest.mark.parametrize("to", ["superseded", "archived"])
def test_terminal_close_of_a_failed_relaunch_pins_its_launch(coord, to):
    # Request B failed before review_ready, so the card names no review request;
    # the pin must name B's launch and the new reason, not keep request A's.
    _reach_second_review(coord)
    card = json.loads(_row(coord, TASK)["card_json"])
    card.pop("terminal_review")
    card["launch_request_id"] = REQ_B
    conn = sqlite3.connect(_db(coord))
    try:
        conn.execute(
            "UPDATE tasks SET status='failed', worker_status='failed', claimed_by=NULL, "
            "started_at=?, card_json=? WHERE task_id=?",
            (NOW, json.dumps(card), TASK),
        )
        conn.commit()
    finally:
        conn.close()

    res = core.reject_review(TASK, NEW_REASON, to=to)

    assert res["ok"] is True, res
    card = json.loads(res["stdout"])
    assert card["rejection_disposition"]["request_id"] == REQ_B
    receipt = server._reject_review_receipt_fields(card, task_id=TASK, reason=NEW_REASON, to=to)
    assert receipt["request_id"] == REQ_B
    assert receipt["reason_identity"]["sha256"] == _sha(NEW_REASON)
    assert not receipt["failure_category"]
