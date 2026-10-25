"""An archived reviewer is never reused for its lens (NF-2026-01692).

A manager archives a reviewer whose report is wrong, and the next
``launch_quality_reviewer`` for that lens must start a fresh reviewer instead of
answering ``reused_existing_reviewer`` with the archived one. A reviewer that is
not archived keeps being reused exactly as before.
"""

from __future__ import annotations

import pytest

from aiworkhub import review_orchestrator

from test_quality_reviewer_launch_reservation import (  # noqa: F401
    ADAPTER,
    RUNNER,
    TOPIC,
    _manager,
    _nf1131_cards,
    _nf1131_reviewer,
    _nf1131_sealed_card,
    _reserve_nf1131,
    _review_ready_target,
    _starting_count,
)

ARCHIVED_AT = "2026-10-25T10:00:00+00:00"
IDENTITY = {
    "target_task_id": "TARGET_TASK",
    "target_request_id": "target-req",
    "claim_epoch": "2",
    "lens": "correctness",
}


def _sealed_manager(tmp_path, monkeypatch, **card):
    manager = _manager(tmp_path)
    _nf1131_reviewer(manager, "done-a", "REVIEWER_A", "review_ready")
    _nf1131_cards(
        monkeypatch,
        {"REVIEWER_A": _nf1131_sealed_card("REVIEWER_A", "done-a", **card)},
    )
    return manager


@pytest.mark.parametrize("refuse", [True, False], ids=["mcp", "automation"])
def test_archived_sealed_reviewer_is_not_reused_and_a_fresh_one_launches(
    tmp_path, monkeypatch, refuse,
):
    manager = _sealed_manager(tmp_path, monkeypatch, archived_at=ARCHIVED_AT)

    assert manager.existing_lens_reviewer(**IDENTITY) is None
    attempts = manager._lens_reviewer_attempts(
        manager._latest_by_request(), "TARGET_TASK", "target-req", "correctness"
    )
    assert attempts
    assert review_orchestrator.existing_lens_reviewer(
        manager.repo,
        target_task_id="TARGET_TASK",
        target_request_id="target-req",
        claim_epoch="2",
        lens="correctness",
        attempts=attempts,
    ) is None

    receipt = _reserve_nf1131(manager, "REVIEWER_B", refuse=refuse)
    assert receipt["ok"] is True
    assert receipt["already_reserved"] is False
    assert "reused_existing_reviewer" not in receipt
    assert _starting_count(manager, "REVIEWER_B") == 1


@pytest.mark.parametrize("refuse", [True, False], ids=["mcp", "automation"])
def test_unarchived_sealed_reviewer_is_still_reused(tmp_path, monkeypatch, refuse):
    manager = _sealed_manager(tmp_path, monkeypatch)

    found = manager.existing_lens_reviewer(**IDENTITY)
    assert found is not None
    assert (found["task_id"], found["request_id"], found["state"]) == (
        "REVIEWER_A", "done-a", "sealed",
    )

    receipt = _reserve_nf1131(manager, "REVIEWER_B", refuse=refuse)
    assert receipt["ok"] is True
    assert receipt["reused_existing_reviewer"] is True
    assert receipt["request_id"] == "done-a"
    assert receipt["task_id"] == "REVIEWER_A"
    assert _starting_count(manager, "REVIEWER_B") == 0


@pytest.mark.parametrize(
    ("card_extra", "expected"),
    [({}, "running"), ({"archived_at": ARCHIVED_AT}, None)],
    ids=["live", "archived_live"],
)
def test_archived_live_reviewer_is_not_reused(tmp_path, monkeypatch, card_extra, expected):
    _nf1131_cards(
        monkeypatch,
        {"REVIEWER_A": {"task_id": "REVIEWER_A", "status": "processing", **card_extra}},
    )
    event = {
        "request_id": "live-a", "task_id": "REVIEWER_A", "runner": RUNNER,
        "topic": TOPIC, "adapter_id": ADAPTER, "state": "running", "model": "",
        "quality_review_attempt": {
            "target_task_id": "TARGET_TASK", "target_request_id": "target-req",
            "lens": "correctness", "target_claim_epoch": "2",
        },
    }
    found = review_orchestrator.existing_lens_reviewer(
        tmp_path,
        target_task_id="TARGET_TASK",
        target_request_id="target-req",
        claim_epoch="2",
        lens="correctness",
        attempts=[("live-a", event, True)],
    )
    assert (found or {}).get("state") == expected
