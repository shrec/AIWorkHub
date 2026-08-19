"""NF-2026-00305: a finalizer whose card is archived/not-processing must reach a
terminal state and stop re-arming, and a launch-target read that loses to
finalization writers must name the task-queue contention distinctly.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import process_launcher, task_store


def _make_manager(tmp_path: Path, **kwargs):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "process_log.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
        **kwargs,
    )


def _seed(mgr, request_id: str, task_id: str, runner: str, state: str) -> None:
    mgr._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": "task_mcp",
        "state": state,
    })


def _raise_no_terminal_event(*_a, **_k):
    raise RuntimeError("no_terminal_event")


# --------------------------------------------------------------------------- #
# Defect one: the finalizer must terminate instead of re-arming forever.
# --------------------------------------------------------------------------- #

def test_archived_finalizer_terminates_and_does_not_rearm(tmp_path, monkeypatch):
    """An archived card yields ``not_processing``; the finalizer must reach a
    terminal ``finalize_abandoned`` state and never re-arm -- proven by counting
    finalization attempts across a window of reconcile passes rather than one
    call."""
    monkeypatch.setattr(process_launcher.time, "sleep", lambda *a, **k: None)
    mgr = _make_manager(tmp_path)
    rid, tid, runner = "req-archived", "TASK-ARCHIVED", "worker-x"
    _seed(mgr, rid, tid, runner, "reconcile_pending")

    attempts = {"n": 0}

    def _never_terminal(request_id, supervisor_returncode=None, *, lock_blocking=True):
        attempts["n"] += 1
        raise RuntimeError("no_terminal_event")

    monkeypatch.setattr(mgr, "_finalize_isolated_request", _never_terminal)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *a, **k: {"ok": False, "stderr": "not_processing:current=pending"},
    )

    # Watch a window of reconcile passes: attempts may climb during the first
    # pass (the bounded retry loop) but must plateau afterwards -- no re-arm.
    counts = []
    for _ in range(5):
        mgr._reconcile_persisted_requests()
        counts.append(attempts["n"])

    assert counts[0] == 3, "the bounded retry loop runs exactly once"
    assert counts == [3, 3, 3, 3, 3], "no re-arm: the attempt count stops climbing"

    latest = mgr._latest_by_request()[rid]
    assert latest["state"] == "finalize_abandoned"
    assert latest["state"] in process_launcher.TERMINAL_PROCESS_STATES
    assert latest["state"] not in process_launcher.FINALIZATION_PENDING_STATES
    assert "not_processing" in latest["error"]

    # Terminal and visible to an operator: a countable list, not a climbing
    # timestamp.
    abandoned = mgr.abandoned_finalizations()
    assert [a["request_id"] for a in abandoned] == [rid]
    assert abandoned[0]["task_id"] == tid
    assert "finalize_abandoned" in abandoned[0]["reason"]


def test_not_processing_finalizer_refuses_by_name(tmp_path, monkeypatch):
    """A card that is not processing produces a named terminal refusal rather
    than a ``reconcile_pending`` retry."""
    monkeypatch.setattr(process_launcher.time, "sleep", lambda *a, **k: None)
    mgr = _make_manager(tmp_path)
    rid, tid, runner = "req-np", "TASK-NP", "worker-y"
    _seed(mgr, rid, tid, runner, "finalizing")

    monkeypatch.setattr(mgr, "_finalize_isolated_request", _raise_no_terminal_event)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *a, **k: {"ok": False, "stderr": "not_processing:current=archived"},
    )

    event = mgr._finalize_after_process_exit(rid)
    assert event["state"] == "finalize_abandoned"
    assert event["state"] not in process_launcher.FINALIZATION_PENDING_STATES
    assert event.get("finalizer_abandoned") is True
    assert "finalize_abandoned:not_processing" in event["error"]


def test_still_processing_finalizer_keeps_its_retry(tmp_path, monkeypatch):
    """The retry exists for a real reason: a card that may still be processing
    (a transient transition conflict, not a ``not_processing`` signal) must keep
    ``reconcile_pending`` and NOT be abandoned."""
    monkeypatch.setattr(process_launcher.time, "sleep", lambda *a, **k: None)
    mgr = _make_manager(tmp_path)
    rid, tid, runner = "req-live", "TASK-LIVE", "worker-z"
    _seed(mgr, rid, tid, runner, "finalizing")

    monkeypatch.setattr(mgr, "_finalize_isolated_request", _raise_no_terminal_event)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *a, **k: {"ok": False, "stderr": "terminal_failure_transition_conflict"},
    )

    event = mgr._finalize_after_process_exit(rid)
    assert event["state"] == "reconcile_pending"
    assert event["state"] in process_launcher.FINALIZATION_PENDING_STATES
    assert event.get("finalizer_abandoned") is False
    assert mgr.abandoned_finalizations() == []


# --------------------------------------------------------------------------- #
# Defect two: a starved launch-target read must name the queue contention.
# --------------------------------------------------------------------------- #

def test_launch_target_read_names_queue_contention_distinctly(tmp_path):
    """When the launch path's target-card read loses to a finalization writer
    storm, it must report contention distinctly -- naming the task queue --
    not a bare ``database is locked`` nor a generic invalid-target error."""

    def _locked_show_task(task_id):
        raise sqlite3.OperationalError("database is locked")

    mgr = process_launcher.ProcessManager(
        repo=tmp_path / "repo_review",
        process_log_path=tmp_path / "plog.jsonl",
        process_dir=tmp_path / "proc2",
        show_task=_locked_show_task,
        isolation_enabled=False,
    )
    rid, tid = "req-review", "TASK-REVIEW"
    mgr._append_event({
        "request_id": rid,
        "task_id": tid,
        "runner": "r",
        "topic": "task_mcp",
        "state": "review_ready",
    })

    result = mgr._build_quality_review_packet(rid, tid)
    assert result["ok"] is False
    assert "contended" in result["error"]
    assert "task_queue" in result["error"]
    # The whole point: distinct from the generic error that hid the real cause.
    assert "quality_review_target_invalid" not in result["error"]


def test_read_target_card_names_contention_but_reads_otherwise(tmp_path, monkeypatch):
    """The bounded target reader returns the card when the queue is free, names
    a lock as contention, and does not misreport an unrelated error as one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready

    tid, runner = "TASK-TARGET", "worker-r"
    now = "2026-08-18T00:00:00+00:00"
    card = {"schema_id": "aiworkhub.task_card.v1", "task_id": tid, "runner": runner}
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, "
            "origin_thread_id, archived_at) VALUES "
            "(?, ?, 'task_mcp', 'pending', 'unclaimed', 'normal', 'obj', ?, ?, ?, '', '')",
            (tid, runner, json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    # Free queue: the card reads back.
    got = task_store.read_target_card(repo, tid)
    assert got is not None and got["task_id"] == tid

    # Pin readiness (which itself opens connections) so the patched connect
    # below only stands in for the reader's own bounded acquisition, modelling a
    # writer holding the queue when the target read runs.
    ready_tuple = task_store._require_ready(repo)
    monkeypatch.setattr(task_store, "_require_ready", lambda root: ready_tuple)

    # A queue lock is named as contention, not surfaced bare.
    def _locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(task_store, "_connect", _locked)
    with pytest.raises(task_store.TaskQueueContended) as excinfo:
        task_store.read_target_card(repo, tid, contention_timeout_ms=100)
    assert "task_queue_contended" in str(excinfo.value)

    # An unrelated operational error is not misreported as contention.
    def _other(*_a, **_k):
        raise sqlite3.OperationalError("no such column: bogus")

    monkeypatch.setattr(task_store, "_connect", _other)
    with pytest.raises(sqlite3.OperationalError) as other_info:
        task_store.read_target_card(repo, tid, contention_timeout_ms=100)
    assert "task_queue_contended" not in str(other_info.value)
