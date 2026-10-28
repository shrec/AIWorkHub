"""NF-2026-01764: a cleanup failure after a committed reject is deferred, not raised.

Measured 2026-10-27: ``aiworkhub_task_reject_review`` returned ``[WinError 5]
Access is denied`` although the reject had committed (task pending, amendments
and feedback recorded).  The exception came from the post-commit reviewer
process cleanup (``ProcessManager.cancel`` reached through
``cancel_disposed_reviewer_processes``) while a Windows handle still held the
reviewer's files, and it propagated past the lesson close-out, so the lesson
was never committed.

These tests run the real ``core.reject_review`` transition, the real learning
commit and the real ``ProcessManager.cancel`` against a real process ledger.
Only one OS-facing callee at a time is made to fail; the fabricated reviewer
pid is never signalled.
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
    core,
    feature_settings,
    learning_commit_store,
    process_event_ledger,
    process_launcher,
    server,
    task_store,
)

NOW = "2026-10-27T00:00:00+00:00"
SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfb"
TASK_ID = "T_MAIN"
REQUEST_ID = "REQ_A"
REVIEWER_TASK = "R_LENS"
REVIEWER_REQUEST = "REQ_REVIEWER"
REVIEWER_PID = 4_000_001  # fabricated: the termination primitive is recorded, never run
REASON = "the cleanup failure must not hide the committed reject"
LESSON = "a post-commit cleanup failure is deferred and never skips the lesson"
EVIDENCE = "file:tests/test_reject_review_cleanup_deferred_nf01764.py"


def _held_path(root: Path) -> str:
    return str(root / ".aiworkhub" / "runtime" / "worktrees" / REVIEWER_REQUEST / "worktree")


def _permission_error(path: str | None) -> OSError:
    return PermissionError(13, "Access is denied", path)


def _winerror_5(path: str | None) -> OSError:
    exc = OSError(13, "Access is denied", path, 5)
    if getattr(exc, "winerror", None) is None:  # POSIX: no native winerror slot
        exc.winerror = 5  # type: ignore[attr-defined]
    return exc


ERRORS = [
    pytest.param(_permission_error, None, id="permission-error-13"),
    pytest.param(_winerror_5, 5, id="winerror-5"),
]


def _insert(root: Path, task_id: str, *, topic: str, card: dict[str, Any]) -> None:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", topic, "solo", "review", "review", "normal",
             "obj", json.dumps(card), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(root: Path, table: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(root, changes={"context_graph": True}, expected_revision=0)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {"provider": "codex", "session_id": SESSION_ID, "thread_id": SESSION_ID},
    })
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    # The coordinator token file needs chmod 0600, which the sandbox denies;
    # the capability itself is not what this regression is about.
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda _runner: (True, "test_coordinator")
    )
    _insert(root, TASK_ID, topic="coding", card={
        "allowed_writes": ["src/aiworkhub/process_launcher.py"],
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"request_identity": {
                "request_id": REQUEST_ID, "task_id": TASK_ID,
                "runner": "claude_coding", "topic": "coding",
            }},
        },
    })
    _insert(root, REVIEWER_TASK, topic="quality_review", card={
        "quality_review": {
            "target_task_id": TASK_ID,
            "target_request_id": REQUEST_ID,
            "target_claim_epoch": 1,
        },
    })
    return root


class _Harness:
    """A real ProcessManager with one live reviewer request on a real ledger."""

    def __init__(self, monkeypatch, root: Path) -> None:
        ledger_dir = root / "process_ledger"
        ledger_dir.mkdir()
        self.manager = process_launcher.ProcessManager(
            repo=root,
            process_log_path=ledger_dir / "processes.jsonl",
            process_dir=ledger_dir / "requests",
        )
        identity = {
            "task_id": REVIEWER_TASK, "runner": "claude_coding",
            "topic": "quality_review", "adapter_id": "claude_coding",
        }
        self.manager._append_event({
            **identity, "request_id": REVIEWER_REQUEST, "state": "running",
            "pid": REVIEWER_PID,
            "quality_review": {"target_task_id": TASK_ID, "target_request_id": REQUEST_ID},
        })
        self.manager._live[REVIEWER_REQUEST] = process_launcher._LiveProcess(
            request_id=REVIEWER_REQUEST, model=None,
            process=type("_Proc", (), {"pid": REVIEWER_PID})(),  # type: ignore[arg-type]
            stdout_path=ledger_dir / "reviewer.stdout",
            stderr_path=ledger_dir / "reviewer.stderr",
            started_at=NOW, timeout_seconds=0, isolated=False, **identity,
        )
        self.terminated: list[int] = []
        self.monkeypatch = monkeypatch

        def record_termination(pid: int, **_kwargs: Any) -> bool:
            self.terminated.append(pid)
            return True

        monkeypatch.setattr(process_launcher, "terminate_process_tree", record_termination)
        monkeypatch.setattr(process_launcher, "default_manager", lambda: self.manager)

    def hold_workspace(self, make_error: Any, path: str) -> None:
        """The cancel's terminal row lands, then its filesystem step meets a held handle."""
        real_append = process_event_ledger.append_event

        def append_then_held(ledger: Path, event: dict[str, Any]) -> dict[str, Any]:
            written = real_append(ledger, event)
            if event.get("request_id") == REVIEWER_REQUEST and event.get("state") == "cancelled":
                raise make_error(path)
            return written

        self.monkeypatch.setattr(process_event_ledger, "append_event", append_then_held)

    def fail_termination(self, make_error: Any) -> None:
        """The OS refuses termination: no path, nothing a janitor can recover."""

        def refuse(pid: int, **_kwargs: Any) -> bool:
            self.terminated.append(pid)
            raise make_error(None)

        self.monkeypatch.setattr(process_launcher, "terminate_process_tree", refuse)

    def reviewer_states(self) -> list[str]:
        return [str(row.get("state")) for row in self.manager._request_events(REVIEWER_REQUEST)]


@pytest.mark.parametrize("make_error, winerror", ERRORS)
def test_cleanup_failure_after_commit_is_deferred_and_the_lesson_lands(
    repo, monkeypatch, make_error, winerror
):
    harness = _Harness(monkeypatch, repo)
    harness.hold_workspace(make_error, _held_path(repo))

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON,
        lesson_candidate=LESSON, extra_evidence_ids=[EVIDENCE],
    )

    assert reply["ok"] is True, reply
    assert harness.terminated == [REVIEWER_PID], "termination ran once, never retried"
    assert harness.reviewer_states() == ["running", "cancelled"], "terminal cancel row landed once"
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"
    assert {row["task_id"] for row in reply["reviewer_finalization"]} == {REVIEWER_TASK}
    assert reply["cleanup_deferred"] == [{
        "path": _held_path(repo), "errno": 13, "winerror": winerror,
        "stage": "workspace_cleanup",
    }]
    cancellation = reply["reviewer_process_cancellation"]
    assert cancellation["cleanup_deferred"] == reply["cleanup_deferred"]
    assert cancellation["cancelled"][0]["state"] == "cleanup_deferred"

    commit = reply["learning_commit"]
    assert commit["ok"] is True, commit
    assert (commit["outcome"], commit["request_id"]) == ("rejected", REQUEST_ID)
    assert len(_rows(repo, "learning_commits")) == 1
    stored = learning_commit_store.read_disposition(
        repo, task_id=TASK_ID, request_id=REQUEST_ID
    )
    assert stored["disposition"] == "lesson_committed"

    rerun = server.aiworkhub_manager_learning_commit(
        task_id=TASK_ID, request_id=REQUEST_ID,
        lesson_candidate=LESSON, extra_evidence_ids=[EVIDENCE],
    )
    assert rerun["ok"] is True, rerun
    assert rerun["idempotent"] is True, "the lesson was already applied"
    assert len(_rows(repo, "learning_commits")) == 1


def test_a_failure_outside_the_workspace_is_reviewer_cancel_failed(repo, monkeypatch):
    harness = _Harness(monkeypatch, repo)
    harness.fail_termination(_winerror_5)

    reply = harness.manager.reject_review(TASK_ID, REASON)

    assert reply["ok"] is True, reply
    assert harness.terminated == [REVIEWER_PID]
    assert harness.reviewer_states() == ["running"], "no terminal row: termination failed"
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"
    assert reply["cleanup_deferred"] == [{
        "path": f"request:{REVIEWER_REQUEST}", "errno": 13, "winerror": 5,
        "stage": "reviewer_cancel_failed",
    }]
    row = reply["reviewer_process_cancellation"]["cancelled"][0]
    assert (row["state"], row["blocked_reason"]) == ("cancel_failed", "reviewer_cancel_failed")


def test_process_manager_reject_review_defers_a_snapshot_read_failure(repo, monkeypatch):
    harness = _Harness(monkeypatch, repo)

    def unreadable_ledger():
        raise _permission_error(_held_path(repo))

    monkeypatch.setattr(harness.manager, "_latest_by_request_stable", unreadable_ledger)

    reply = harness.manager.reject_review(TASK_ID, REASON)

    assert reply["ok"] is True, reply
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"
    assert reply["reviewer_process_cancellation"]["state"] == "cleanup_deferred"
    assert reply["reviewer_process_cancellation"]["cancelled"] == []
    assert reply["cleanup_deferred"] == [{
        "path": _held_path(repo), "errno": 13, "winerror": None,
        "stage": "reviewer_process_cancellation",
    }]
    assert harness.terminated == []


def test_the_server_defers_a_snapshot_read_failure_too(repo, monkeypatch):
    harness = _Harness(monkeypatch, repo)

    def unreadable_ledger():
        raise _permission_error(_held_path(repo))

    monkeypatch.setattr(harness.manager, "_latest_by_request_stable", unreadable_ledger)

    reply = server.aiworkhub_task_reject_review(task_id=TASK_ID, reason=REASON)

    assert reply["ok"] is True, reply
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"
    assert reply["cleanup_deferred"][0]["path"] == _held_path(repo)
    assert reply["reviewer_process_cancellation"]["state"] == "cleanup_deferred"


def test_a_refusal_before_the_transition_is_unchanged(repo, monkeypatch):
    harness = _Harness(monkeypatch, repo)
    harness.hold_workspace(_permission_error, _held_path(repo))

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, to="not-a-disposition",
        lesson_candidate=LESSON,
    )

    assert reply["ok"] is False
    assert "cleanup_deferred" not in reply
    assert "learning_commit" not in reply
    assert harness.terminated == []
    assert harness.reviewer_states() == ["running"]
    assert task_store.get_task(repo, TASK_ID)["status"] == "review"
    assert _rows(repo, "tasks") and not [
        row for row in _rows(repo, "task_events") if row["event"] == "reject_review"
    ]
