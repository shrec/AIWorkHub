"""NF-2026-02234: relaunching a reviewer whose card already exists.

A reviewer card requeued to pending by ``retry_terminal`` used to make
``launch_quality_reviewer`` fail with
``quality_review_task_create_failed:task_already_exists`` forever.  The
launcher now relaunches that exact pending card, and never duplicates a card
that already produced a review for the same target and lens.
"""

from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher, quality_reviewer


def _quality_reviewer_launch_harness(tmp_path, monkeypatch):
    """A self-contained ProcessManager whose task store is the ``cards`` dict."""

    cards: dict[str, dict] = {}
    order: list[str] = []
    started: list[object] = []

    def show(task_id: str):
        stored = cards.get(str(task_id))
        if isinstance(stored, dict) and stored.get("topic") == "quality_review":
            return {"returncode": 0, "stdout": json.dumps(stored), "stderr": ""}
        card = {
            "task_id": task_id,
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "review",
            "terminal_review": {"substatus": "review_ready"},
            "allowed_writes": [],
            "priority": "high",
        }
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    repo = tmp_path / "repo"
    repo.mkdir()

    def build_plan(**_kwargs):
        return SimpleNamespace(
            argv=[sys.executable, "-c", "pass"],
            cwd=str(repo),
            launchable=True,
            reason="",
        )

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show,
        collision_guard=lambda **_k: {
            "returncode": 0,
            "stdout": '{"collision_free":true}',
            "stderr": "",
        },
        adapter_builder=build_plan,
        isolation_enabled=False,
    )
    monkeypatch.setattr(
        process_launcher.quality_review,
        "assess_reviewer_launch_target",
        lambda **_k: {
            "can_launch": True,
            "fails_at_launch": False,
            "reason": "reviewer_target_review_ready",
            "target_substatus": "review_ready",
        },
    )

    def fake_claim(repo, task_id, runner, topic, request_id=""):
        order.append("claim")
        existing = cards.get(str(task_id), {})
        card = {
            **existing,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "launch_request_id": request_id,
            "claim_epoch": 1,
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": runner,
            "read_only": existing.get("read_only", True),
            "allowed_writes": list(existing.get("allowed_writes") or []),
        }
        cards[str(task_id)] = card
        return {"ok": True, "returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    class FakeThread:
        def __init__(self, target=None, kwargs=None, name=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.name = name
            started.append(self)

        def start(self):
            order.append("thread")

    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", fake_claim)
    monkeypatch.setattr(process_launcher.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_exact_quality_review_target_refusal",
        lambda self, target_request_id, target_task_id: None,
    )
    return manager, cards, order, started

REVIEWER_TASK_ID = "QUALITY_REVIEW_NF02234"
TARGET_TASK_ID = "TARGET_TASK_1"
RUNNER = "claude_worker_reviewer"
LENS = "correctness"


def _existing_card(*, lens: str = LENS, **overrides) -> dict:
    card = {
        "task_id": REVIEWER_TASK_ID,
        "runner": RUNNER,
        "topic": "quality_review",
        "read_only": True,
        "allowed_writes": [],
        "title": quality_reviewer.reviewer_card_title(
            lens=lens, target_task_id=TARGET_TASK_ID
        ),
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": None,
        "terminal_retry": {
            "schema_id": "aiworkhub.terminal_retry.v1",
            "task_id": REVIEWER_TASK_ID,
            "request_id": "prior-worker-failed-request",
            "terminal_substatus": "worker_failed",
        },
    }
    card.update(overrides)
    return card


def _harness(tmp_path, monkeypatch, existing: dict | None):
    manager, cards, order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )
    if existing is not None:
        cards[REVIEWER_TASK_ID] = copy.deepcopy(existing)
    creates: list[dict] = []
    wakes: list[str] = []

    def create_like_core(**kwargs):
        order.append("create")
        creates.append(kwargs)
        task_id = str(kwargs["task_id"])
        if task_id in cards:
            return {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": f"task_already_exists:{task_id}",
            }
        cards[task_id] = {
            "task_id": task_id,
            "runner": kwargs["runner"],
            "topic": kwargs["topic"],
            "title": kwargs["title"],
            "read_only": kwargs.get("read_only", False),
            "allowed_writes": list(kwargs.get("allowed_writes") or []),
            "status": "pending",
            "worker_status": "unclaimed",
        }
        return {"ok": True, "created": True, "task_id": task_id}

    monkeypatch.setattr(process_launcher.core, "create_task", create_like_core)
    monkeypatch.setattr(
        process_launcher.core,
        "release_review_wake",
        lambda _task, reason: wakes.append(reason),
    )
    return manager, cards, order, started, creates, wakes


def _launch(manager):
    return manager.launch_quality_reviewer(
        target_request_id="target-req-1",
        target_task_id=TARGET_TASK_ID,
        reviewer_task_id=REVIEWER_TASK_ID,
        runner=RUNNER,
        adapter_id="claude_cli",
        lens=LENS,
    )


def test_pending_requeued_reviewer_card_is_relaunched(tmp_path, monkeypatch):
    manager, cards, order, started, creates, wakes = _harness(
        tmp_path, monkeypatch, _existing_card()
    )

    receipt = _launch(manager)

    assert receipt["ok"] is True, receipt
    assert receipt["task_id"] == REVIEWER_TASK_ID
    assert order == ["create", "claim", "thread"]
    assert len(started) == 1
    assert started[0].kwargs["reviewer_task_id"] == REVIEWER_TASK_ID
    card = cards[REVIEWER_TASK_ID]
    assert card["status"] == "processing"
    assert card["launch_request_id"] == receipt["request_id"]
    assert wakes == []
    assert len(creates) == 1


def test_fresh_reviewer_card_title_comes_from_the_single_owner(tmp_path, monkeypatch):
    manager, cards, _order, _started, creates, _wakes = _harness(
        tmp_path, monkeypatch, None
    )

    receipt = _launch(manager)

    assert receipt["ok"] is True, receipt
    expected = quality_reviewer.reviewer_card_title(
        lens=LENS, target_task_id=TARGET_TASK_ID
    )
    assert [kw["title"] for kw in creates] == [expected]
    assert expected == f"Independent {LENS} review for {TARGET_TASK_ID}"


@pytest.mark.parametrize(
    ("overrides", "existing_status"),
    [
        (
            {
                "status": "review",
                "worker_status": "review",
                "terminal_substatus": "review_ready",
            },
            "review_ready",
        ),
        (
            {
                "status": "review",
                "worker_status": "review",
                "terminal_review": {"substatus": "review_ready"},
            },
            "review_ready",
        ),
        ({"status": "accepted", "worker_status": "accepted"}, "accepted"),
    ],
)
def test_reviewed_card_is_not_duplicated_and_is_named(
    tmp_path, monkeypatch, overrides, existing_status
):
    existing = _existing_card(**overrides)
    manager, cards, order, started, creates, wakes = _harness(
        tmp_path, monkeypatch, existing
    )

    receipt = _launch(manager)

    assert receipt["ok"] is False
    assert receipt["error"] == (
        f"quality_review_card_already_reviewed:{existing_status}"
    )
    assert receipt["existing_task_id"] == REVIEWER_TASK_ID
    assert receipt["existing_status"] == existing_status
    assert receipt["task_id"] == REVIEWER_TASK_ID
    assert "claim" not in order
    assert "thread" not in order
    assert started == []
    assert len(creates) == 1
    assert cards[REVIEWER_TASK_ID] == existing
    assert wakes == [receipt["error"]]


@pytest.mark.parametrize(
    ("existing", "reason"),
    [
        (
            _existing_card(lens="security"),
            "existing_reviewer_card_identity_mismatch:target_or_lens",
        ),
        (
            _existing_card(runner="some_other_runner"),
            "existing_reviewer_card_identity_mismatch:runner",
        ),
        (
            _existing_card(read_only=False),
            "existing_reviewer_card_identity_mismatch:read_only",
        ),
        (
            _existing_card(review_target_task_id="OTHER_TARGET"),
            "existing_reviewer_card_identity_mismatch:review_target_task_id",
        ),
        (
            _existing_card(claimed_by=RUNNER),
            "existing_reviewer_card_not_reusable:pending/unclaimed",
        ),
        (
            _existing_card(status="blocked", worker_status="worker_failed"),
            "existing_reviewer_card_not_reusable:blocked/worker_failed",
        ),
    ],
)
def test_unreusable_existing_card_still_fails_closed(
    tmp_path, monkeypatch, existing, reason
):
    manager, cards, order, started, _creates, wakes = _harness(
        tmp_path, monkeypatch, existing
    )

    receipt = _launch(manager)

    assert receipt["ok"] is False
    error = str(receipt.get("error") or "")
    assert error.startswith("quality_review_task_create_failed:task_already_exists")
    assert error.endswith(reason)
    assert "claim" not in order
    assert started == []
    assert cards[REVIEWER_TASK_ID] == existing
    assert wakes == [error]


def test_disposition_refuses_unreadable_card():
    verdict = quality_reviewer.existing_reviewer_card_disposition(
        None,
        reviewer_task_id=REVIEWER_TASK_ID,
        target_task_id=TARGET_TASK_ID,
        lens=LENS,
        runner=RUNNER,
    )
    assert verdict == {
        "disposition": quality_reviewer.EXISTING_REVIEWER_CARD_REFUSED,
        "reason": "existing_reviewer_card_unreadable",
    }


def test_disposition_reuses_only_the_exact_pending_card():
    verdict = quality_reviewer.existing_reviewer_card_disposition(
        _existing_card(),
        reviewer_task_id=REVIEWER_TASK_ID,
        target_task_id=TARGET_TASK_ID,
        lens=LENS,
        runner=RUNNER,
    )
    assert verdict["disposition"] == quality_reviewer.EXISTING_REVIEWER_CARD_REUSE
    other = quality_reviewer.existing_reviewer_card_disposition(
        _existing_card(),
        reviewer_task_id="QUALITY_REVIEW_SOMETHING_ELSE",
        target_task_id=TARGET_TASK_ID,
        lens=LENS,
        runner=RUNNER,
    )
    assert other["disposition"] == quality_reviewer.EXISTING_REVIEWER_CARD_REFUSED
    assert other["reason"].endswith("task_id")


class _ReadRefused(RuntimeError):
    pass


def _failure(created: dict, read_card):
    return quality_reviewer.existing_reviewer_card_failure(
        created, "detail", lambda reason: {"ok": False, "error": reason}, read_card,
        (_ReadRefused,), REVIEWER_TASK_ID, TARGET_TASK_ID, lens=LENS, runner=RUNNER,
    )


def test_failure_helper_keeps_plain_create_failures_without_reading_the_card():
    def never(_task_id):
        raise AssertionError("card must not be read")

    for stderr in ("disk_full", "task_already_exists:SOME_OTHER_CARD"):
        result = _failure({"ok": False, "stderr": stderr}, never)
        assert result == {"ok": False, "error": "quality_review_task_create_failed:detail"}


def test_failure_helper_reuses_pending_and_fails_closed_on_unreadable_card():
    created = {"ok": False, "stderr": f"task_already_exists:{REVIEWER_TASK_ID}"}
    assert _failure(created, lambda _t: _existing_card()) is None

    def refused(_task_id):
        raise _ReadRefused("show failed")

    result = _failure(created, refused)
    assert result == {
        "ok": False,
        "error": "quality_review_task_create_failed:detail:existing_reviewer_card_unreadable",
    }
