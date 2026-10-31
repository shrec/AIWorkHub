"""NF-2026-01892: a non-claiming peek + host-side watcher for callbacks."""

from __future__ import annotations

import json
import time

import pytest

from aiworkhub import callback_store, manager_callback_watch

SESSION = "claude-session-A"
OTHER_SESSION = "claude-session-B"


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "task_queue.sqlite"
    monkeypatch.setattr(callback_store, "resolve_db_path", lambda repo: path)
    # The claim's stale-member check needs a populated tasks table; the
    # selection under test does not.
    monkeypatch.setattr(callback_store, "_task_still_in_matching_terminal_state", lambda *a, **k: True)
    conn = callback_store.open_db(path)
    try:
        callback_store._ensure_callback_outbox_table(conn)
        callback_store._ensure_callback_batches_table(conn)
    finally:
        conn.close()
    return path


def _enqueue_raw(path, task_id, thread=SESSION, created="2026-01-01T00:00:00+00:00"):
    conn = callback_store.open_db(path)
    try:
        conn.execute(
            "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, transition, "
            "request_id, state, created_at, updated_at) VALUES (?, 'claude', ?, 'review_ready', ?, "
            "'pending', ?, ?)",
            (task_id, thread, f"req-{task_id}", created, created),
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(conn):
    return (
        [tuple(r) for r in conn.execute("SELECT * FROM callback_outbox ORDER BY outbox_id")],
        [tuple(r) for r in conn.execute("SELECT * FROM callback_batches ORDER BY batch_id")],
    )


def test_peek_none_without_tables(tmp_path):
    conn = callback_store.open_db(tmp_path / "empty.sqlite")
    try:
        assert callback_store.peek_claimable_callback(conn, provider="claude", origin_thread_id=SESSION) is None
    finally:
        conn.close()


def test_peek_mutates_nothing_and_claim_returns_same_members(db_path):
    _enqueue_raw(db_path, "T1")
    _enqueue_raw(db_path, "T2", created="2026-01-01T00:00:01+00:00")
    _enqueue_raw(db_path, "TX", thread=OTHER_SESSION)
    conn = callback_store.open_db(db_path)
    try:
        before = _snapshot(conn)
        peeked = callback_store.peek_claimable_callback(conn, provider="claude", origin_thread_id=SESSION)
        assert _snapshot(conn) == before
        assert [m["task_id"] for m in peeked["members"]] == ["T1", "T2"]
        assert peeked["members"][0] == {"task_id": "T1", "request_id": "req-T1", "state": "review_ready"}

        claimed = callback_store.claim_pending_callback_batch(conn, provider="claude", origin_thread_id=SESSION)
        assert [m["task_id"] for m in claimed["members"]] == ["T1", "T2"]
        assert [m["request_id"] for m in claimed["members"]] == [m["request_id"] for m in peeked["members"]]
    finally:
        conn.close()


def test_peek_existing_batch_and_expired_lease_match_claim(db_path):
    _enqueue_raw(db_path, "T1")
    conn = callback_store.open_db(db_path)
    try:
        first = callback_store.claim_pending_callback_batch(
            conn, lease_seconds=1, provider="claude", origin_thread_id=SESSION
        )
        # Still leased: nothing is claimable and peek agrees.
        assert callback_store.peek_claimable_callback(conn, provider="claude", origin_thread_id=SESSION) is None
        conn.execute(
            "UPDATE callback_batches SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE batch_id=?",
            (first["batch_id"],),
        )
        conn.commit()
        before = _snapshot(conn)
        peeked = callback_store.peek_claimable_callback(conn, provider="claude", origin_thread_id=SESSION)
        assert _snapshot(conn) == before
        assert peeked["batch_id"] == first["batch_id"]
        again = callback_store.claim_pending_callback_batch(conn, provider="claude", origin_thread_id=SESSION)
        assert again["batch_id"] == first["batch_id"]
        assert [m["task_id"] for m in again["members"]] == [m["task_id"] for m in peeked["members"]]
    finally:
        conn.close()


def test_watch_exits_zero_with_ids_and_does_not_claim(db_path, capsys):
    _enqueue_raw(db_path, "T1")
    started = time.monotonic()
    code = manager_callback_watch.main(
        ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION,
         "--timeout", "3", "--poll-interval", "0.1"]
    )
    assert code == 0
    assert time.monotonic() - started < 3
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "callback_pending"
    assert [m["task_id"] for m in payload["members"]] == ["T1"]
    conn = callback_store.open_db(db_path)
    try:
        assert conn.execute("SELECT state, batch_id FROM callback_outbox").fetchall()[0][:] == ("pending", "")
        assert callback_store.claim_pending_callback_batch(
            conn, provider="claude", origin_thread_id=SESSION
        )["members"][0]["task_id"] == "T1"
    finally:
        conn.close()


def test_watch_times_out_and_ignores_other_origin(db_path, capsys):
    _enqueue_raw(db_path, "TX", thread=OTHER_SESSION)
    code = manager_callback_watch.main(
        ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION,
         "--timeout", "0.3", "--poll-interval", "0.1"]
    )
    assert code == 3
    assert json.loads(capsys.readouterr().out.strip()) == {"status": "timeout"}


def test_watch_reports_store_error(monkeypatch, capsys):
    def _boom(repo):
        raise callback_store.CallbackStoreNotReadyError("not_initialized")

    monkeypatch.setattr(callback_store, "resolve_db_path", _boom)
    code = manager_callback_watch.main(
        ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION, "--timeout", "1"]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out.strip())["status"] == "error"
