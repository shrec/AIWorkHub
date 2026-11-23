"""NF-2026-02357: a reviewer bound to a rejected round never starts.

A quality reviewer created for rework round N must not launch once round N was
rejected and round N+1 reached ``review_ready``: its worktree is gone, so the
prewarm fails ``candidate_path_missing`` and the card was left BLOCKED.  The
target identity is checked at launch entry and again right before prewarm, a
stale reviewer is finalized SUPERSEDED (never blocked), and the reject path
also supersedes pending / admission-deferred reviewer cards with no process.

Fakes only: no provider is ever started.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import (
    process_launcher,
    process_launcher_launch_isolated,
    quality_evidence,
    task_engine,
    task_store,
)

LENS = sorted(quality_evidence.JUDGMENT_LENSES)[0]
NOW = "2026-11-23T00:00:00+00:00"


def _result(card: dict) -> dict:
    return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}


def _target_card(current_request_id: str) -> dict:
    return {
        "task_id": "T_MAIN",
        "status": "review",
        "worker_status": "review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"request_identity": {"request_id": current_request_id}},
        },
    }


def _reviewer_card(task_id: str, target_request_id: str | None, **fields) -> dict:
    card = {
        "task_id": task_id,
        "topic": "quality_review",
        "status": "pending",
        "worker_status": "unclaimed",
        "review_target_task_id": "T_MAIN",
        **fields,
    }
    if target_request_id is not None:
        card["quality_review"] = {
            "target_task_id": "T_MAIN",
            "target_request_id": target_request_id,
        }
    return card


def _manager(cards: dict[str, dict], tmp_path: Path) -> process_launcher.ProcessManager:
    manager = object.__new__(process_launcher.ProcessManager)
    manager.repo = tmp_path
    manager.process_dir = tmp_path

    def show(task_id: str) -> dict:
        if task_id in cards:
            return _result(cards[task_id])
        return {"returncode": 1, "stdout": "", "stderr": "task_not_found"}

    manager._show_task = show  # type: ignore[method-assign]
    return manager


@pytest.fixture
def archived(monkeypatch):
    calls: list[dict] = []

    def archive(root, task_id, **kwargs):
        calls.append({"task_id": task_id, **kwargs})
        return True, "superseded"

    monkeypatch.setattr(task_store, "archive_task", archive)
    # Never a blocked card: the launch-failure release must not run.
    monkeypatch.setattr(
        task_engine, "mark_launch_failed",
        lambda *_a, **_k: pytest.fail("stale reviewer must not be marked blocked"),
    )
    monkeypatch.setattr(
        task_engine, "record_launch_blocker",
        lambda *_a, **_k: pytest.fail("stale reviewer must not record a blocker"),
    )
    return calls


def _launch(manager) -> dict:
    return manager.launch_quality_reviewer(
        target_request_id="REQ_N",
        target_task_id="T_MAIN",
        reviewer_task_id="R_LENS",
        runner="claude_coding",
        adapter_id="claude_cli",
        lens=LENS,
    )


def test_launch_refuses_review_ready_target_whose_current_request_differs(
    monkeypatch, tmp_path, archived
):
    cards = {
        "T_MAIN": _target_card("REQ_N1"),
        "R_LENS": _reviewer_card(
            "R_LENS", "REQ_N", transient_retry={"kind": "admission_deferred"}
        ),
    }
    manager = _manager(cards, tmp_path)
    monkeypatch.setattr(manager, "_live_reviewer_receipt", lambda *_a, **_k: None)
    # The stale-target gate runs only after the ledger proved REQ_N is a
    # request of T_MAIN.
    ledger_checked: list[tuple] = []
    monkeypatch.setattr(
        manager, "_exact_quality_review_target_refusal",
        lambda *args: ledger_checked.append(args),
    )
    monkeypatch.setattr(
        manager, "_reserve_quality_reviewer_attempt",
        lambda **_k: pytest.fail("stale target must never reserve a reviewer"),
    )

    result = _launch(manager)

    assert result["ok"] is False
    assert result["error"] == "quality_review_target_superseded"
    assert result["reason"] == "quality_review_target_superseded:REQ_N"
    assert result["current_target_request_id"] == "REQ_N1"
    assert result["fails_at_launch"] is True
    assert result["request_created"] is False
    assert result["reviewer_card_superseded"]["superseded"] is True
    assert [(c["task_id"], c["operation"]) for c in archived] == [("R_LENS", "superseded")]
    assert ledger_checked == [("REQ_N", "T_MAIN")]


def test_pre_prewarm_recheck_finalizes_stale_reviewer_superseded(tmp_path, archived):
    binding = {"target_task_id": "T_MAIN", "target_request_id": "REQ_N"}
    cards = {
        # The reject landed after launch entry: round N+1 is now current.
        "T_MAIN": _target_card("REQ_N1"),
        # The reviewer's own in-flight claim: processing, no card binding.
        "R_LENS": _reviewer_card(
            "R_LENS", None, status="processing", worker_status="claimed"
        ),
    }
    manager = _manager(cards, tmp_path)

    with pytest.raises(process_launcher.LaunchRejected) as caught:
        process_launcher_launch_isolated.recheck_reviewer_target(manager, binding)
    reason = str(caught.value)
    assert reason == "quality_review_target_superseded:REQ_N"

    assert process_launcher_launch_isolated.finalize_superseded_reviewer(
        manager, "R_LENS", reason, binding
    ) is True
    assert archived == [{
        "task_id": "R_LENS",
        "actor": "aiworkhub_process_launcher",
        "reason": reason,
        "allow_processing": True,
        "operation": "superseded",
    }]
    # Any other refusal keeps the ordinary launch-failure path.
    assert process_launcher_launch_isolated.finalize_superseded_reviewer(
        manager, "R_LENS", "quality_review_source_graph_prewarm_failed:x", binding
    ) is False
    assert len(archived) == 1

    # The re-check sits before prewarm and the routing before the release.
    source = inspect.getsource(process_launcher_launch_isolated.launch_isolated)
    assert source.index("recheck_reviewer_target(self") < source.index(
        "prewarm_quality_review_source_graph("
    )
    assert source.index("finalize_superseded_reviewer(") < source.index(
        "_release_or_retain_reviewer_claim_after_launch_failure("
    )


def test_pre_prewarm_recheck_passes_current_target(tmp_path):
    manager = _manager({"T_MAIN": _target_card("REQ_N")}, tmp_path)
    process_launcher_launch_isolated.recheck_reviewer_target(
        manager, {"target_task_id": "T_MAIN", "target_request_id": "REQ_N"}
    )


def _insert(root: Path, card: dict) -> None:
    conn = sqlite3.connect(task_store.storage_readiness(root).canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (card["task_id"], "claude_coding", card["topic"], "solo", card["status"],
             card["worker_status"], "normal", "obj", json.dumps(card), NOW, NOW, ""),
        )
        conn.commit()
    finally:
        conn.close()


def _status(root: Path, task_id: str) -> str:
    conn = sqlite3.connect(task_store.storage_readiness(root).canonical_db)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0])


def test_reject_supersedes_queued_reviewer_cards_with_no_process(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    deferred = {"kind": "admission_deferred", "request_id": ""}
    _insert(root, _reviewer_card("R_QUEUED", "R_REJ", transient_retry=deferred))
    _insert(root, _reviewer_card("R_OTHER", "R_DIFFERENT", transient_retry=deferred))
    # Bound only through a terminal ledger row (admission refusal), no card binding.
    _insert(root, _reviewer_card("R_LEDGER", None))

    manager = object.__new__(process_launcher.ProcessManager)
    manager.repo = root
    manager.process_dir = tmp_path

    def show(task_id: str) -> dict:
        card = task_store.get_task(root, task_id)
        if not card:
            return {"returncode": 1, "stdout": "", "stderr": "task_not_found"}
        return _result({**card, "task_id": task_id})

    manager._show_task = show  # type: ignore[method-assign]
    latest = {
        "REQ_LEDGER": {
            "task_id": "R_LEDGER",
            "topic": "quality_review",
            "state": "blocked",
            "quality_review": {"target_request_id": "R_REJ"},
        },
    }
    monkeypatch.setattr(manager, "_latest_by_request_stable", lambda: (latest, ("stable",)))
    monkeypatch.setattr(
        manager, "cancel",
        lambda *_a, **_k: pytest.fail("no reviewer here owns a live process"),
    )

    result = manager.cancel_disposed_reviewer_processes([], rejected_request_id="R_REJ")

    assert result["ok"] is True
    assert result["cancelled"] == []
    assert {row["task_id"] for row in result["superseded_cards"]} == {"R_QUEUED", "R_LEDGER"}
    assert all(row["superseded"] is True for row in result["superseded_cards"])
    assert _status(root, "R_QUEUED") == "superseded"
    assert _status(root, "R_LEDGER") == "superseded"
    assert _status(root, "R_OTHER") == "pending"


def test_reviewer_for_current_review_request_still_launches(monkeypatch, tmp_path, archived):
    cards = {"T_MAIN": _target_card("REQ_N")}
    manager = _manager(cards, tmp_path)
    monkeypatch.setattr(manager, "_live_reviewer_receipt", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "_exact_quality_review_target_refusal", lambda *_a: None)
    reserved: list[dict] = []

    def reserve(**kwargs):
        reserved.append(kwargs)
        return {"ok": True, "reused_existing_reviewer": True, "request_id": "RQ_EXISTING"}

    monkeypatch.setattr(manager, "_reserve_quality_reviewer_attempt", reserve)

    result = _launch(manager)

    assert result == {"ok": True, "reused_existing_reviewer": True, "request_id": "RQ_EXISTING"}
    assert len(reserved) == 1 and reserved[0]["target_request_id"] == "REQ_N"
    assert archived == []


def _stale_launch_manager(monkeypatch, tmp_path):
    cards = {
        "T_MAIN": _target_card("REQ_N1"),
        "R_LENS": _reviewer_card("R_LENS", "REQ_N"),
    }
    manager = _manager(cards, tmp_path)
    monkeypatch.setattr(manager, "_live_reviewer_receipt", lambda *_a, **_k: None)
    monkeypatch.setattr(
        manager, "_reserve_quality_reviewer_attempt",
        lambda **_k: pytest.fail("a refused target must never reserve a reviewer"),
    )
    return manager


def test_made_up_target_request_is_refused_by_ledger_without_supersede(
    monkeypatch, tmp_path, archived
):
    manager = _stale_launch_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "_request_events", lambda _request_id: [])
    monkeypatch.setattr(manager, "_latest_by_request", lambda: {})

    result = _launch(manager)

    assert result["ok"] is False
    assert result["error"] == "quality_review_target_request_not_found"
    assert result["request_created"] is False
    assert "reviewer_card_superseded" not in result
    assert archived == []


def test_request_of_another_task_supersedes_nothing(monkeypatch, tmp_path, archived):
    manager = _stale_launch_manager(monkeypatch, tmp_path)
    # REQ_N is a real ledger request, but of task T_OTHER, not T_MAIN.
    monkeypatch.setattr(
        manager, "_request_events",
        lambda _request_id: [{"request_id": "REQ_N", "task_id": "T_OTHER", "state": "exited"}],
    )

    result = _launch(manager)

    assert result["ok"] is False
    assert result["error"] == "quality_review_target_request_task_mismatch"
    assert "reviewer_card_superseded" not in result
    assert archived == []

    # The card binding names T_MAIN; a supersede for target task T_B is refused.
    outcome = manager._supersede_stale_reviewer_card(
        "R_LENS", reason="x", target_request_id="REQ_N", target_task_id="T_B"
    )
    assert outcome == {
        "task_id": "R_LENS", "target_request_id": "REQ_N",
        "ok": True, "superseded": False, "state": "bound_to_other_target",
    }
    assert archived == []


def _reject_manager(monkeypatch, tmp_path, cards, latest):
    manager = _manager(cards, tmp_path)
    monkeypatch.setattr(manager, "_latest_by_request_stable", lambda: (latest, ("stable",)))
    monkeypatch.setattr(
        manager, "cancel",
        lambda *_a, **_k: pytest.fail("no reviewer here owns a live process"),
    )
    return manager


def test_archive_failure_inside_reject_pass_never_raises(monkeypatch, tmp_path):
    cards = {"R_Q": _reviewer_card("R_Q", "R_REJ")}
    manager = _reject_manager(monkeypatch, tmp_path, cards, {})
    monkeypatch.setattr(manager, "_queued_reviewer_task_ids", lambda _r: ["R_Q"])

    def archive_raises(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(task_store, "archive_task", archive_raises)

    result = manager.cancel_disposed_reviewer_processes([], rejected_request_id="R_REJ")

    assert result["ok"] is False
    assert result["cancelled"] == []
    assert [row["state"] for row in result["superseded_cards"]] == [
        "archive_failed:OperationalError"
    ]

    # A failure anywhere in the pass is named, never raised.
    def queued_raises(_request_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(manager, "_queued_reviewer_task_ids", queued_raises)
    result = manager.cancel_disposed_reviewer_processes([], rejected_request_id="R_REJ")
    assert result["ok"] is False
    assert result["cancelled"] == []
    assert result["superseded_cards"] == [{
        "task_id": "", "target_request_id": "R_REJ", "ok": False,
        "superseded": False, "state": "supersede_pass_failed:RuntimeError",
    }]


def test_reused_reviewer_is_owned_only_by_its_newest_request(
    monkeypatch, tmp_path, archived
):
    cards = {"R_REUSED": _reviewer_card("R_REUSED", None)}
    latest = {
        # Ledger order is not age: the newest request is listed first.
        "REQ_NEW": {
            "task_id": "R_REUSED", "topic": "quality_review", "state": "blocked",
            "timestamp": "2026-11-23T02:00:00+00:00",
            "quality_review": {"target_request_id": "R_N1"},
        },
        "REQ_OLD": {
            "task_id": "R_REUSED", "topic": "quality_review", "state": "blocked",
            "timestamp": "2026-11-23T01:00:00+00:00",
            "quality_review": {"target_request_id": "R_REJ"},
        },
    }
    manager = _reject_manager(monkeypatch, tmp_path, cards, latest)
    monkeypatch.setattr(manager, "_queued_reviewer_task_ids", lambda _r: [])

    result = manager.cancel_disposed_reviewer_processes([], rejected_request_id="R_REJ")

    assert result["ok"] is True
    assert "superseded_cards" not in result
    assert archived == []


def test_superseded_launch_finalization_returns_superseded_not_launch_failed(tmp_path):
    manager = object.__new__(process_launcher.ProcessManager)
    appended: list[dict] = []
    manager._append_event = lambda event: appended.append(event) or event  # type: ignore[method-assign]
    reason = "quality_review_target_superseded:REQ_N"

    result = process_launcher_launch_isolated.superseded_reviewer_result(
        manager, "R_LENS", "claude_coding", "quality_review", "claude_cli", reason,
        request_id="RQ_RESERVED", claimed=True, diagnostic=None,
    )

    assert result["ok"] is False
    assert result["state"] == "superseded"
    assert result["error"] == process_launcher.quality_review.REVIEWER_TARGET_SUPERSEDED
    assert result["request_id"] == "RQ_RESERVED"
    assert result["task_id"] == "R_LENS"
    assert result["blocked_reason"] == reason
    # The ledger row stays a terminal launcher state so the reservation closes.
    assert appended[0]["state"] in process_launcher.TERMINAL_PROCESS_STATES

    # launch_isolated returns that receipt before the launch_failed fallthrough.
    source = inspect.getsource(process_launcher_launch_isolated.launch_isolated)
    routed = source.index("return superseded_reviewer_result(")
    assert routed < source.rindex("return self._blocked(")
    assert source.index("if superseded_reviewer:\n            return") <= routed


def test_terminal_evidence_only_binding_to_other_target_is_not_superseded(
    tmp_path, archived
):
    card = _reviewer_card("R_EVIDENCE", None)
    card["terminal_review"] = {"evidence": {"quality_review": {
        "target_task_id": "T_OTHER", "target_request_id": "REQ_N",
    }}}
    manager = _manager({"R_EVIDENCE": card}, tmp_path)

    outcome = manager._supersede_stale_reviewer_card(
        "R_EVIDENCE", reason="x", target_request_id="REQ_N", target_task_id="T_MAIN"
    )

    assert outcome["state"] == "bound_to_other_target"
    assert outcome["superseded"] is False
    assert archived == []
    binding = process_launcher.quality_review.reviewer_card_binding(card)
    assert binding["target_task_id"] == "T_OTHER"
    assert process_launcher.quality_review.reviewer_card_target_request_id(card) == "REQ_N"


def test_queued_reviewer_ids_prefilter_only_rows_naming_the_request(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    _insert(root, _reviewer_card("R_MATCH", "R_WANTED"))
    _insert(root, _reviewer_card("R_MISS", "R_UNRELATED"))
    manager = object.__new__(process_launcher.ProcessManager)
    manager.repo = root

    assert manager._queued_reviewer_task_ids("R_WANTED") == ["R_MATCH"]
    assert manager._queued_reviewer_task_ids("R_NONE") == []
    assert manager._queued_reviewer_task_ids("") == []
