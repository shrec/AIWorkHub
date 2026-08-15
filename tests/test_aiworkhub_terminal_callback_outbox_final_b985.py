from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store, task_engine, task_store  # noqa: E402


def _repo(tmp_path: Path, *, callback_required: bool = False, episode: int = 1) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _ready, db_path = task_store._require_ready(repo)
    card = {
        "task_id": "TASK_B985",
        "runner": "codex_b985",
        "topic": "task_mcp",
        "callback_required": callback_required,
        "coordinator_provider": "codex",
        "origin_thread_id": "thread-b985",
        "claim_epoch": episode,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at, origin_thread_id) VALUES (?, ?, ?, 'processing', 'claimed', "
            "'', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK_B985", "codex_b985", "task_mcp", json.dumps(card),
                "2026-07-27T00:00:00+00:00", "2026-07-27T00:00:00+00:00",
                "codex_b985", "2026-07-27T00:00:00+00:00",
                "2026-07-27T00:00:00+00:00", "thread-b985",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def _rows(repo: Path) -> list[sqlite3.Row]:
    _ready, db_path = task_store._require_ready(repo)
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        return conn.execute("SELECT * FROM callback_outbox ORDER BY outbox_id").fetchall()
    finally:
        conn.close()


def test_legacy_callback_required_false_still_enqueues_terminal_review(tmp_path: Path) -> None:
    repo = _repo(tmp_path, callback_required=False)
    result = task_engine.mark_terminal_review(
        repo, "TASK_B985", "codex_b985", "validation_failed",
        evidence={"request_id": "req-b985", "transient_error": True},
    )
    assert result["ok"] is True
    assert result["callback_enqueued"] is True
    rows = _rows(repo)
    assert len(rows) == 1
    assert rows[0]["transition"] == "validation_failed"
    card = task_store.get_task(repo, "TASK_B985")
    assert card["terminal_review"]["evidence"]["transient_error"] is True


def test_terminal_review_and_callback_roll_back_together_on_outbox_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, callback_required=True)

    def fail_outbox(*_args: object, **_kwargs: object) -> bool:
        raise sqlite3.OperationalError("injected callback write failure")

    monkeypatch.setattr(task_store, "_enqueue_terminal_callback_row", fail_outbox)
    result = task_engine.mark_terminal_review(
        repo,
        "TASK_B985",
        "codex_b985",
        "review_ready",
        evidence={"request_id": "req-atomic-b985"},
    )

    assert result["ok"] is False
    assert "injected callback write failure" in result["stderr"]
    card = task_store.get_task(repo, "TASK_B985")
    assert card is not None
    assert card["status"] == "processing"
    assert card["worker_status"] == "claimed"
    assert "terminal_review" not in card
    assert task_store.get_task_events(repo, "TASK_B985") == []
    assert _rows(repo) == []


def test_terminal_review_state_event_and_callback_share_one_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, callback_required=True, episode=7)
    result = task_engine.mark_terminal_review(
        repo,
        "TASK_B985",
        "codex_b985",
        "validation_failed",
        evidence={"request_id": "req-atomic-commit"},
    )

    assert result["ok"] is True
    assert result["callback_enqueued"] is True
    rows = _rows(repo)
    assert len(rows) == 1
    assert rows[0]["provider"] == "codex"
    assert rows[0]["episode_id"] == "7"
    assert rows[0]["request_id"] == "req-atomic-commit"
    events = task_store.get_task_events(repo, "TASK_B985")
    assert {event["event"] for event in events} == {
        "terminal_review",
        "callback_enqueued",
    }


def test_unknown_terminal_state_is_rejected_from_outbox(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = task_engine.mark_terminal_review(
        repo, "TASK_B985", "codex_b985", "future_typo",
    )
    assert result["ok"] is False
    assert _rows(repo) == []


def test_review_backlog_reconciliation_is_once_per_route_episode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task_engine.mark_terminal_review(repo, "TASK_B985", "codex_b985", "blocked")
    _ready, db_path = task_store._require_ready(repo)
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        conn.execute("DELETE FROM callback_outbox")
        conn.commit()
        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id="thread-b985"
        ) == 1
        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id="thread-b985"
        ) == 0
        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id="thread-other"
        ) == 1
    finally:
        conn.close()
    assert len(_rows(repo)) == 2


def test_direct_unknown_transition_is_not_enqueued(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _ready, db_path = task_store._require_ready(repo)
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        assert callback_store.enqueue_callback(
            conn, "TASK_B985", "thread-b985", "future_typo",
            provider="codex", episode_id="1",
        ) is False
    finally:
        conn.close()
    assert _rows(repo) == []


def test_verified_route_recovers_matching_dead_letter_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = task_engine.mark_terminal_review(
        repo, "TASK_B985", "codex_b985", "review_ready",
        evidence={"request_id": "req-b985"},
    )
    assert result["ok"] is True
    _ready, db_path = task_store._require_ready(repo)
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        claimed = callback_store.claim_pending_callback_batch(conn, provider="codex")
        assert claimed is not None
        callback_store.mark_batch_dead_letter(
            conn,
            claimed["batch_id"],
            "post_ack_turn_cancelled",
            claimed["lease_id"],
        )

        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id="thread-b985"
        ) == 1
        row = conn.execute(
            "SELECT state,recovery_count,batch_id FROM callback_outbox WHERE task_id='TASK_B985'"
        ).fetchone()
        assert tuple(row) == ("pending", 1, "")

        claimed_again = callback_store.claim_pending_callback_batch(conn, provider="codex")
        assert claimed_again is not None
        callback_store.mark_batch_dead_letter(
            conn,
            claimed_again["batch_id"],
            "real_transport_failure",
            claimed_again["lease_id"],
        )
        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id="thread-b985"
        ) == 0
        row = conn.execute(
            "SELECT state,recovery_count FROM callback_outbox WHERE task_id='TASK_B985'"
        ).fetchone()
        assert tuple(row) == ("dead_letter", 1)
    finally:
        conn.close()
