"""NF-2026-00307: a blocked/terminal card can never be written without a
named cause. The boundary enforces it rather than each caller remembering, and
a path that cannot determine the cause records a name that says exactly that
and identifies the path -- never an empty string, never a mute placeholder.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aiworkhub import task_store

RUNNER = "deepseek_tasking"
TOPIC = "tasking_system"
NOW = "2026-08-19T00:00:00+00:00"


# --- the boundary itself --------------------------------------------------


def test_reason_text_wins_and_is_bounded():
    assert task_store.blocker_reason_or_named_gap("sandbox exploded", path="p") == (
        "sandbox exploded"
    )
    long = "x" * 5000
    assert len(task_store.blocker_reason_or_named_gap(long, path="p")) == 500


def test_absent_reason_falls_back_to_caller_known_cause():
    assert (
        task_store.blocker_reason_or_named_gap("", path="p", fallback="timed_out")
        == "timed_out"
    )
    assert (
        task_store.blocker_reason_or_named_gap(None, path="p", fallback="  ")
        == "cause_undetermined:p"
    )


def test_undeterminable_cause_records_a_named_gap_identifying_the_path():
    named = task_store.blocker_reason_or_named_gap("   ", path="mark_launch_failed")
    # It states it could not be determined AND identifies the exact path, so an
    # operator can tell a real diagnosis from an unfilled one.
    assert named == "cause_undetermined:mark_launch_failed"
    assert named != ""
    # A bare canonical state name would satisfy a non-empty check while
    # informing no one: the named gap must NOT be such a placeholder.
    assert named not in {"blocked", "launch_failed", "unknown"}


# --- the enforcing writers ------------------------------------------------


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return repo


def _insert_processing_claimed(repo: Path, task_id: str, *, launch_request_id: str) -> None:
    readiness = task_store.storage_readiness(repo)
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": RUNNER,
        "launch_request_id": launch_request_id,
        "claim_epoch": 1,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "project_context": {"task_type": "research"},
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                RUNNER,
                TOPIC,
                "solo",
                "processing",
                "claimed",
                "normal",
                "objective",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                NOW,
                NOW,
                RUNNER,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_launch_failed_with_empty_reason_records_a_named_cause(tmp_path, monkeypatch):
    """The path that previously passed nothing now records a real named cause.

    ``mark_launch_failed`` accepts ``reason=""``; before this fix that emptiness
    reached the card verbatim, so recovery started blind. The boundary now
    fills a named cause that identifies the writing path.
    """

    repo = _repo(tmp_path, monkeypatch)
    _insert_processing_claimed(repo, "TASK_BLIND", launch_request_id="request-1")

    ok, state = task_store.mark_launch_failed(
        repo, "TASK_BLIND", runner=RUNNER, reason="", request_id="request-1"
    )
    assert ok is True
    assert state == "blocked"

    card = task_store.get_task(repo, "TASK_BLIND") or {}
    assert card["status"] == "blocked"
    reason = card["blocker_reason"]
    assert reason, "a blocked card must never carry an empty blocker reason"
    assert reason == "cause_undetermined:mark_launch_failed"
    # It also flows into the launch-specific error field, still non-empty.
    assert card["launch_error"] == "cause_undetermined:mark_launch_failed"


def test_launch_failed_preserves_a_real_reason(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert_processing_claimed(repo, "TASK_NAMED", launch_request_id="request-1")

    ok, _state = task_store.mark_launch_failed(
        repo, "TASK_NAMED", runner=RUNNER, reason="adapter binary missing",
        request_id="request-1",
    )
    assert ok is True
    card = task_store.get_task(repo, "TASK_NAMED") or {}
    assert card["blocker_reason"] == "adapter binary missing"
