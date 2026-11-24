"""NF-2026-02411: a queued launch's request id points at the request that relaunched it.

A real ``ProcessManager`` (own ledger under ``tmp_path``) with only ``launch``
replaced by a fake that either refuses for capacity -- through the real
``_admission_deferral`` -- or succeeds with a canned successor id. The real
reconciler scan (``_scan_transient_retry_relaunch``) drives the relaunch.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    dependency_autolaunch,
    launch_admission,
    process_launcher,
    task_reconciler,
    task_store,
)

NOW = "2026-11-20T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"
MEMORY_REASON = 'memory_launch_capacity_denied:{"reason":"memory_capacity_insufficient"}'
QUEUED_ID = "a" * 32
SUCCESSOR_ID = "b" * 32
RUNNER = "claude_coding"


def _insert_pending(root: Path, task_id: str) -> None:
    card = {"task_id": task_id, "runner": RUNNER, "topic": "task_mcp"}
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, "
            "claimed_by, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, RUNNER, "task_mcp", "pending", "unclaimed", None,
             json.dumps(card), NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()


def _card(root: Path, task_id: str) -> dict[str, Any]:
    return task_store.get_tasks(root, [task_id])[task_id]


def _make_due(root: Path, task_id: str) -> None:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        (raw,) = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        card = json.loads(raw)
        card["retry_not_before"] = PAST
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id)
        )
        conn.commit()
    finally:
        conn.close()


class _FakeLauncherManager(process_launcher.ProcessManager):
    """Real ledger and status; ``launch`` is scripted."""

    def __init__(self, repo: Path, tmp: Path) -> None:
        super().__init__(
            repo=repo,
            process_log_path=tmp / "processes.jsonl",
            process_dir=tmp / "processes",
            show_task=lambda _task_id: {},
            isolation_enabled=False,
        )
        self.refuse_ids: list[str] = []  # queued ids minted when a launch is refused
        self.admit = False
        self.launch_calls = 0

    def launch(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.launch_calls += 1
        task_id = str(kwargs["task_id"])
        if self.admit:
            return {"ok": True, "request_id": SUCCESSOR_ID, "task_id": task_id,
                    "state": "starting"}
        reply = self._admission_deferral(
            task_id, str(kwargs["runner"]), str(kwargs["topic"]), "fake_adapter",
            MEMORY_REASON, None,
        )
        assert reply is not None
        self.refuse_ids.append(str(reply["request_id"]))
        return reply


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


@pytest.fixture
def manager(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeLauncherManager:
    monkeypatch.setattr(dependency_autolaunch, "network_reachable", lambda: True)
    state = tmp_path / "state"
    state.mkdir()
    return _FakeLauncherManager(repo, state)


def _queue(manager: _FakeLauncherManager, repo: Path, task_id: str = "T1") -> str:
    """Queue ``task_id`` through the real deferral path; return the queued id."""
    _insert_pending(repo, task_id)
    reply = manager._admission_deferral(
        task_id, RUNNER, "task_mcp", "fake_adapter", MEMORY_REASON, QUEUED_ID
    )
    assert reply is not None and reply["request_id"] == QUEUED_ID
    return QUEUED_ID


def test_defer_card_stores_the_queued_request_id(repo: Path) -> None:
    _insert_pending(repo, "T1")
    assert launch_admission.defer_card(
        repo, "T1", RUNNER, MEMORY_REASON, request_id=QUEUED_ID
    ) == (True, "pending")
    transient = _card(repo, "T1")["transient_retry"]
    assert transient["kind"] == "admission_deferred"
    assert transient["request_id"] == QUEUED_ID


def test_defer_card_without_request_id_keeps_the_old_contract(repo: Path) -> None:
    _insert_pending(repo, "T1")
    assert launch_admission.defer_card(repo, "T1", RUNNER, MEMORY_REASON) == (True, "pending")
    assert _card(repo, "T1")["transient_retry"]["request_id"] == ""


def test_deferral_without_request_id_mints_one_that_matches_the_card(
    manager: _FakeLauncherManager, repo: Path
) -> None:
    _insert_pending(repo, "T1")
    reply = manager._admission_deferral(
        "T1", RUNNER, "task_mcp", "fake_adapter", MEMORY_REASON, None
    )
    assert reply is not None
    assert reply["request_id"]
    assert _card(repo, "T1")["transient_retry"]["request_id"] == reply["request_id"]
    assert manager.status(reply["request_id"])["state"] == "admission_queued"


def test_relaunch_points_the_queued_request_at_its_successor(
    manager: _FakeLauncherManager, repo: Path
) -> None:
    queued = _queue(manager, repo)
    assert manager.status(queued)["state"] == "admission_queued"
    _make_due(repo, "T1")
    manager.admit = True

    outcome = task_reconciler._scan_transient_retry_relaunch(manager)

    assert [row["task_id"] for row in outcome["launched"]] == ["T1"]
    status = manager.status(queued)
    assert status["state"] == "admission_relaunched"
    assert status["successor_request_id"] == SUCCESSOR_ID
    assert status["latest_event"]["successor_request_id"] == SUCCESSOR_ID
    assert status["task_id"] == "T1"
    assert status["runner"] == RUNNER


def test_refused_relaunch_leaves_the_old_id_queued_with_no_successor(
    manager: _FakeLauncherManager, repo: Path
) -> None:
    queued = _queue(manager, repo)
    _make_due(repo, "T1")
    manager.admit = False

    outcome = task_reconciler._scan_transient_retry_relaunch(manager)

    assert outcome["launched"] == []
    assert manager.launch_calls == 1
    status = manager.status(queued)
    assert status["state"] == "admission_queued"
    assert "successor_request_id" not in status
    assert "successor_request_id" not in status["latest_event"]
    # The refusal queued a fresh request, which now owns the card's pointer.
    assert len(manager.refuse_ids) == 1
    assert manager.refuse_ids[0] != queued
    assert _card(repo, "T1")["transient_retry"]["request_id"] == manager.refuse_ids[0]
    assert manager.status(manager.refuse_ids[0])["state"] == "admission_queued"


def test_non_admission_transient_retry_is_never_linked(
    manager: _FakeLauncherManager, repo: Path
) -> None:
    queued = _queue(manager, repo)
    # A failed-attempt retry: same request_id field, different kind.
    conn = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        (raw,) = conn.execute("SELECT card_json FROM tasks WHERE task_id='T1'").fetchone()
        card = json.loads(raw)
        card["transient_retry"]["kind"] = "provider_transient"
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id='T1'", (json.dumps(card),))
        conn.commit()
    finally:
        conn.close()
    _make_due(repo, "T1")
    manager.admit = True

    task_reconciler._scan_transient_retry_relaunch(manager)

    assert manager.status(queued)["state"] == "admission_queued"


def test_record_admission_relaunch_is_idempotent_and_guarded(
    manager: _FakeLauncherManager, repo: Path
) -> None:
    queued = _queue(manager, repo)
    assert manager.record_admission_relaunch("", SUCCESSOR_ID) is None
    assert manager.record_admission_relaunch(queued, "") is None
    assert manager.record_admission_relaunch(queued, queued) is None
    assert manager.record_admission_relaunch("c" * 32, SUCCESSOR_ID) is None
    assert manager.status(queued)["state"] == "admission_queued"
    assert manager.record_admission_relaunch(queued, SUCCESSOR_ID) is not None
    # A second link would overwrite the pointer; the state guard refuses it.
    assert manager.record_admission_relaunch(queued, "d" * 32) is None
    assert manager.status(queued)["successor_request_id"] == SUCCESSOR_ID


def test_defer_card_still_never_raises_on_store_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(task_store, "mark_admission_deferred", boom)
    deferred, detail = launch_admission.defer_card(
        repo, "T1", RUNNER, MEMORY_REASON, request_id=QUEUED_ID
    )
    assert deferred is False
    assert detail == "admission_deferral_failed:OperationalError"
