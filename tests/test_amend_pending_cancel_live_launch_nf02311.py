"""NF-2026-02311: task_amend_pending(cancel_live_launch=True) for a claimed card.

A claimed/processing auto-rework whose fix needs a path outside its
allowed_writes used to burn a whole run: amend_pending refused card_claimed /
card_already_launched. With ``cancel_live_launch=True`` the manager cancels the
card's launch (terminal state required), the card returns to pending and
unclaimed with ``cancelled_launch`` recorded, and the amendment lands in the
same call. The flag off keeps every refusal; a non-terminal cancel changes
nothing; an archived card stays refused.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, manager_card_amend, process_launcher, task_store  # noqa: E402

TEST_FILE = "tests/test_widget.py"
VALIDATION = f"pytest -q {TEST_FILE}"
REQUEST_ID = "req-live-9"
STAMP = "2026-01-01T00:00:00+00:00"


class _FakeManager:
    def __init__(self, state: str = "cancelled", *, raises: Exception | None = None) -> None:
        self.state = state
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def cancel(self, request_id: str, reason: str = "owner_cancelled") -> dict[str, Any]:
        self.calls.append((request_id, reason))
        if self.raises is not None:
            raise self.raises
        return {"ok": True, "request_id": request_id, "state": self.state}


@pytest.fixture
def manager_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / TEST_FILE).write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    task_store.initialize_repository(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "test_route"))
    return root


def _use_manager(monkeypatch: pytest.MonkeyPatch, manager: _FakeManager) -> _FakeManager:
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    return manager


CLAIMED = {
    "status": "processing",
    "worker_status": "claimed",
    "claimed_by": "claude-worker",
    "started_at": STAMP,
    "launch_request_id": REQUEST_ID,
}
LAUNCHED_PENDING = {"started_at": STAMP, "launch_request_id": REQUEST_ID}


def _insert_card(repo: Path, task_id: str, **overrides: Any) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    card: dict[str, Any] = {
        "task_id": task_id,
        "runner": "claude-worker",
        "topic": "coding",
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": "do the thing",
        "allowed_writes": ["src/widget.py"],
        "read_first": [],
        "forbidden": [],
        "validation": [VALIDATION],
        "validation_roles": ["generic"],
        "work_kind": "generic",
        "history": [{"event": "created"}, {"event": "claimed", "request_id": REQUEST_ID}],
    }
    card.update(overrides)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at, started_at, claimed_by) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                task_id, card["runner"], card["topic"], card["status"], card["worker_status"],
                json.dumps(card), STAMP, STAMP, str(overrides.get("archived_at") or ""),
                overrides.get("started_at"), overrides.get("claimed_by"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> dict[str, Any]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, worker_status, claimed_by, archived_at, started_at, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def _stored(repo: Path, task_id: str) -> dict[str, Any]:
    return json.loads(_row(repo, task_id)["card_json"])


def _events(repo: Path, task_id: str) -> list[str]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def test_fake_states_match_the_launcher_vocabulary() -> None:
    assert "cancelled" in process_launcher.TERMINAL_PROCESS_STATES
    assert "cancel_requested" not in process_launcher.TERMINAL_PROCESS_STATES


# ------------------------------------------------------------- reproduction


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [(CLAIMED, "card_claimed"), (LAUNCHED_PENDING, "card_already_launched")],
)
def test_reproduction_cancel_live_launch_returns_card_to_pending_and_amends(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], refusal: str
) -> None:
    _insert_card(manager_repo, "T_LIVE", **overrides)
    before = _stored(manager_repo, "T_LIVE")
    assert task_store.unlaunched_pending_refusal(_row(manager_repo, "T_LIVE"), before) == refusal
    manager = _use_manager(monkeypatch, _FakeManager("cancelled"))

    result = manager_card_amend.amend_pending(
        "T_LIVE", allowed_writes=[TEST_FILE], cancel_live_launch=True
    )

    assert result["ok"] is True, result
    assert result["state"] == "amended"
    assert result["cancelled_request_id"] == REQUEST_ID
    assert result["allowed_writes_added"] == [TEST_FILE]
    assert manager.calls == [(REQUEST_ID, "manager_cancel_for_amend")]
    row = _row(manager_repo, "T_LIVE")
    assert (row["status"], row["worker_status"]) == ("pending", "unclaimed")
    assert not row["claimed_by"]
    card = json.loads(row["card_json"])
    assert card["allowed_writes"] == ["src/widget.py", TEST_FILE]
    assert card["status"] == "pending" and card["worker_status"] == "unclaimed"
    assert "claimed_by" not in card
    cancelled = card[task_store.CANCELLED_LAUNCH_KEY]
    assert cancelled["request_id"] == REQUEST_ID
    assert cancelled["reason"] == "manager_cancel_for_amend"
    assert cancelled["cancelled_at"]
    assert task_store._live_launch_request(card) is False
    assert task_store.unlaunched_pending_refusal(row, card) == ""
    assert [entry["event"] for entry in card["history"][-2:]] == [
        task_store.CARD_LAUNCH_CANCELLED_FOR_AMEND_EVENT,
        task_store.CARD_AMENDED_PENDING_EVENT,
    ]
    assert _events(manager_repo, "T_LIVE") == [
        task_store.CARD_LAUNCH_CANCELLED_FOR_AMEND_EVENT,
        task_store.CARD_AMENDED_PENDING_EVENT,
    ]


def test_server_tool_threads_cancel_live_launch(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import server

    _insert_card(manager_repo, "T_SRV", **CLAIMED)
    _use_manager(monkeypatch, _FakeManager("cancelled"))

    result = server.aiworkhub_task_amend_pending(
        "T_SRV", allowed_writes=[TEST_FILE], cancel_live_launch=True
    )

    assert result["ok"] is True, result
    assert result["cancelled_request_id"] == REQUEST_ID
    stored = task_store.get_task(manager_repo, "T_SRV")
    assert result["card_sha256"] == server._card_identity(stored)["card_sha256"]


def test_live_launch_request_reads_only_a_matching_cancelled_launch() -> None:
    card = {"launch_request_id": REQUEST_ID, "cancelled_launch": {"request_id": "req-other"}}
    assert task_store._live_launch_request(card) is True
    card["cancelled_launch"] = {"request_id": REQUEST_ID}
    assert task_store._live_launch_request(card) is False


# --------------------------------------------------------------- regression


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [(CLAIMED, "card_claimed"), (LAUNCHED_PENDING, "card_already_launched")],
)
def test_flag_false_keeps_the_refusal_and_cancels_nothing(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], refusal: str
) -> None:
    _insert_card(manager_repo, "T_OFF", **overrides)
    before = _row(manager_repo, "T_OFF")
    manager = _use_manager(monkeypatch, _FakeManager("cancelled"))

    result = manager_card_amend.amend_pending("T_OFF", allowed_writes=[TEST_FILE])

    assert result["ok"] is False
    assert result["error"] == f"task_amend_pending_refused:{refusal}"
    assert "cancelled_request_id" not in result
    assert manager.calls == []
    assert _row(manager_repo, "T_OFF") == before


@pytest.mark.parametrize("state", ["cancel_requested", "running", ""])
def test_non_terminal_cancel_refuses_and_leaves_card_unchanged(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _insert_card(manager_repo, "T_NT", **CLAIMED)
    before = _row(manager_repo, "T_NT")
    manager = _use_manager(monkeypatch, _FakeManager(state))

    result = manager_card_amend.amend_pending(
        "T_NT", allowed_writes=[TEST_FILE], cancel_live_launch=True
    )

    assert result["ok"] is False
    assert result["error"] == (
        f"task_amend_pending_refused:cancel_not_terminal:{state or 'unknown'}"
    )
    assert manager.calls == [(REQUEST_ID, "manager_cancel_for_amend")]
    assert _row(manager_repo, "T_NT") == before
    assert _events(manager_repo, "T_NT") == []


def test_cancel_exception_refuses_by_name_and_leaves_card_unchanged(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_card(manager_repo, "T_EXC", **CLAIMED)
    before = _row(manager_repo, "T_EXC")
    _use_manager(monkeypatch, _FakeManager(raises=OSError("registry unreadable")))

    result = manager_card_amend.amend_pending(
        "T_EXC", allowed_writes=[TEST_FILE], cancel_live_launch=True
    )

    assert result["ok"] is False
    assert result["error"] == "task_amend_pending_refused:cancel_failed:OSError"
    assert _row(manager_repo, "T_EXC") == before


def test_archived_card_is_refused_even_with_cancel_flag(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_card(manager_repo, "T_ARC", archived_at="2026-01-03T00:00:00+00:00", **CLAIMED)
    before = _row(manager_repo, "T_ARC")
    manager = _use_manager(monkeypatch, _FakeManager("cancelled"))

    result = manager_card_amend.amend_pending(
        "T_ARC", allowed_writes=[TEST_FILE], cancel_live_launch=True
    )

    assert result["ok"] is False
    assert result["error"] == "task_amend_pending_refused:card_archived"
    assert manager.calls == []
    assert _row(manager_repo, "T_ARC") == before


def test_review_card_is_not_cancelled(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_card(
        manager_repo, "T_REV", status="review", worker_status="review",
        launch_request_id=REQUEST_ID,
    )
    manager = _use_manager(monkeypatch, _FakeManager("cancelled"))

    result = manager_card_amend.amend_pending(
        "T_REV", allowed_writes=[TEST_FILE], cancel_live_launch=True
    )

    assert result["error"] == "task_amend_pending_refused:card_not_pending:review"
    assert manager.calls == []


def test_store_refuses_when_the_launch_request_changed(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_CAS", **CLAIMED)
    before = _row(manager_repo, "T_CAS")

    ok, reason, detail = task_store.return_cancelled_launch_to_pending(
        manager_repo, "T_CAS", actor="claude", request_id="req-stale",
        reason="manager_cancel_for_amend",
    )

    assert (ok, reason, detail) == (False, "launch_request_changed", {})
    assert _row(manager_repo, "T_CAS") == before


@pytest.mark.parametrize(
    ("overrides", "kwargs"),
    [
        ({}, {"allowed_writes": [".git/config"]}),
        ({"read_only": True}, {"allowed_writes": [TEST_FILE]}),
        ({}, {"validation": ["pytest -q a && rm -rf b"]}),
    ],
    ids=["bad_path", "read_only", "bad_command"],
)
def test_invalid_amendment_is_refused_before_the_launch_is_cancelled(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any], kwargs: dict[str, Any],
) -> None:
    _insert_card(manager_repo, "T_BAD", **{**CLAIMED, **overrides})
    before = _row(manager_repo, "T_BAD")
    manager = _use_manager(monkeypatch, _FakeManager("cancelled"))

    result = manager_card_amend.amend_pending("T_BAD", cancel_live_launch=True, **kwargs)

    assert result["ok"] is False
    assert "cancelled_request_id" not in result
    # The running work survives an amendment that would be refused anyway.
    assert manager.calls == []
    assert _row(manager_repo, "T_BAD") == before
    assert _events(manager_repo, "T_BAD") == []
