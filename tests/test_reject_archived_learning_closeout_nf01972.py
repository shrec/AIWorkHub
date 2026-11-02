"""NF-2026-01972: a reject to=archived closes its learning out against the reviewed launch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aiworkhub import core, learning_commit, learning_commit_store, server, task_store
from test_learning_disposition_wiring_nf01672 import (
    REASON,
    REJECTED_PATHS,
    TASK_ID,
    _assert_nothing_recorded,
    _review_card,
    _seed,
    _stored,
    decisions,  # noqa: F401 -- fixture
)

LAUNCH_ID = "request-launch-archived-0001"
EVENT_ID = "request-review-ready-0002"
NO_NEW_LESSON = "mechanical_change_only"


class _ArchiveReject:
    """A reject engine that archives the card and writes no rework fields."""

    def __init__(self, root: Any, *, launch_request_id: str | None, owed: bool) -> None:
        self.root = root
        self.launch_request_id = launch_request_id
        self.owed_record: dict[str, Any] | None = None
        self.owed = owed

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        card = {**_review_card(), "status": "archived", "worker_status": "archived"}
        if self.launch_request_id:
            card["launch_request_id"] = self.launch_request_id
        _seed(self.root, card)
        result: dict[str, Any] = {
            "ok": True,
            "returncode": 0,
            "command": ["taskctl", "reject-review", kwargs["task_id"]],
            "stdout": json.dumps(task_store.get_task(self.root, kwargs["task_id"])),
            "stderr": "",
            "reviewer_finalization": [],
        }
        if self.owed:
            self.owed_record = learning_commit.commit_owed(
                task_id=kwargs["task_id"],
                request_id=self.launch_request_id or "",
                outcome="rejected",
                changed_paths=REJECTED_PATHS,
            )
            result["learning_commit_owed"] = dict(self.owed_record)
        return result


def _archive(monkeypatch, decisions, **engine: Any) -> _ArchiveReject:  # noqa: F811
    fake = _ArchiveReject(decisions.root, **engine)
    monkeypatch.setattr(core, "reject_review", fake)
    return fake


def _reject_archived(**learning: Any) -> dict[str, Any]:
    return server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, to="archived", **learning
    )


def test_an_archived_reject_records_the_disposition_against_the_launch(
    decisions, monkeypatch  # noqa: F811
):
    fake = _archive(monkeypatch, decisions, launch_request_id=LAUNCH_ID, owed=False)

    reply = _reject_archived(no_new_lesson_reason=NO_NEW_LESSON)

    assert reply["ok"] is True, reply
    assert reply["to"] == "archived"
    assert reply["request_id"] == LAUNCH_ID
    assert fake.owed_record is None
    disposition = reply["learning_disposition"]
    assert disposition["ok"] is True, disposition
    stored = _stored(decisions.root, LAUNCH_ID)
    assert stored is not None
    assert stored["disposition"] == learning_commit_store.DISPOSITION_NO_NEW_LESSON
    assert decisions.cancellations[-1]["rejected_request_id"] == LAUNCH_ID


def test_an_archived_reject_falls_back_to_the_latest_launch_event(
    decisions, monkeypatch  # noqa: F811
):
    _archive(monkeypatch, decisions, launch_request_id=None, owed=False)
    events = {"review_ready": {"request_id": EVENT_ID}}
    monkeypatch.setattr(
        server, "_latest_task_event_payload", lambda _task, name: events.get(name)
    )

    reply = _reject_archived(no_new_lesson_reason=NO_NEW_LESSON)

    assert reply["ok"] is True, reply
    assert reply["request_id"] == EVENT_ID
    assert reply["learning_disposition"]["ok"] is True, reply["learning_disposition"]
    stored = _stored(decisions.root, EVENT_ID)
    assert stored["disposition"] == learning_commit_store.DISPOSITION_NO_NEW_LESSON


@pytest.mark.parametrize(
    "learning, key",
    [
        pytest.param({"lesson_candidate": "never launched"}, "learning_commit", id="lesson"),
        pytest.param({"no_new_lesson_reason": NO_NEW_LESSON}, "learning_disposition", id="reason"),
    ],
)
def test_a_never_launched_card_reports_the_closeout_as_skipped(
    decisions, monkeypatch, learning, key  # noqa: F811
):
    _archive(monkeypatch, decisions, launch_request_id=None, owed=False)

    reply = _reject_archived(**learning)

    assert reply["ok"] is True, reply
    assert reply["request_id"] is None
    assert reply[key] == {"skipped": True, "reason": "no_launch_request_for_task"}
    assert "learning_commit_owed" not in reply
    _assert_nothing_recorded(decisions.root)


def test_a_skipped_closeout_leaves_the_owed_record_exactly_as_it_was(
    decisions, monkeypatch  # noqa: F811
):
    fake = _archive(monkeypatch, decisions, launch_request_id=None, owed=True)

    reply = _reject_archived(no_new_lesson_reason=NO_NEW_LESSON)

    assert reply["ok"] is True, reply
    assert reply["learning_disposition"] == {
        "skipped": True,
        "reason": "no_launch_request_for_task",
    }
    assert reply["learning_commit_owed"] == fake.owed_record
    _assert_nothing_recorded(decisions.root)


def test_accept_closeout_keeps_the_unresolved_error_shape():
    # Only the reject wrapper opts into the skipped shape; accept is unchanged.
    receipt = server._with_learning_closeout(
        {"ok": True},
        task_id=TASK_ID,
        lesson_candidate=None,
        promote_ai_memory=False,
        extra_evidence_ids=None,
        no_new_lesson_reason=NO_NEW_LESSON,
    )

    assert receipt["learning_disposition"] == {
        "ok": False,
        "error": "learning_closeout_request_id_unresolved",
    }
