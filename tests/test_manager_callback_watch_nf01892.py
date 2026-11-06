"""NF-2026-01892: a non-claiming peek + host-side watcher for callbacks."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

import pytest

from aiworkhub import callback_store, db_writer, manager_callback_watch, tool_guard_hook

SESSION = "claude-session-A"
OTHER_SESSION = "claude-session-B"


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "task_queue.sqlite"
    # ``--repo .`` anchors the watcher heartbeat (NF-2026-02110): keep it in tmp_path.
    monkeypatch.chdir(tmp_path)
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


def test_watch_reports_store_error(tmp_path, monkeypatch, capsys):
    def _boom(repo):
        raise callback_store.CallbackStoreNotReadyError("not_initialized")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(callback_store, "resolve_db_path", _boom)
    code = manager_callback_watch.main(
        ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION, "--timeout", "1"]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out.strip())["status"] == "error"


def test_watch_never_holds_write_lease_while_polling(db_path):
    result: dict = {}

    def _run() -> None:
        result["code"] = manager_callback_watch.main(
            ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION,
             "--timeout", "5", "--poll-interval", "0.05"]
        )

    watcher = threading.Thread(target=_run, daemon=True)
    watcher.start()
    try:
        time.sleep(0.3)
        assert watcher.is_alive()
        with db_writer.write_lease(db_path, timeout_s=1.0):
            pass
        _enqueue_raw(db_path, "T1")
        watcher.join(timeout=5.0)
        assert not watcher.is_alive()
        assert result["code"] == manager_callback_watch.EXIT_PENDING
    finally:
        # --timeout bounds the watcher, so this join always ends it before tmp cleanup.
        watcher.join(timeout=10.0)


# --- NF-2026-02110: the watcher heartbeat the manager's Stop guard reads ---


def _heartbeat_path(tmp_path):
    path = tool_guard_hook.manager_watch_heartbeat_path(tmp_path.resolve(), SESSION)
    assert path is not None
    return path


def _argv(timeout, poll="0.05"):
    return ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION,
            "--timeout", timeout, "--poll-interval", poll]


def _processing_task_queue(root):
    """A task queue at the Stop guard's own path holding one processing card."""

    path = root.joinpath(*tool_guard_hook.TASK_QUEUE_DB_PARTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, card_json TEXT, "
            "origin_thread_id TEXT, archived_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO tasks (task_id, status, card_json, origin_thread_id) "
            "VALUES ('T9', 'processing', '{}', ?)",
            (SESSION,),
        )
        conn.commit()
    finally:
        conn.close()


def test_watch_heartbeat_is_kept_while_polling_and_removed_on_timeout(db_path, tmp_path, capsys):
    heartbeat = _heartbeat_path(tmp_path)
    _processing_task_queue(tmp_path)
    stop_payload = {"session_id": SESSION, "stop_hook_active": False}
    result: dict = {}

    def _run() -> None:
        result["code"] = manager_callback_watch.main(_argv("1.5"))

    watcher = threading.Thread(target=_run, daemon=True)
    watcher.start()
    try:
        deadline = time.monotonic() + 5.0
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        record = json.loads(heartbeat.read_text(encoding="utf-8"))
        assert record["pid"] == os.getpid()
        assert record["poll_interval"] == 0.05
        first = datetime.fromisoformat(record["updated_at"])
        assert first.tzinfo is not None
        time.sleep(0.3)
        later = datetime.fromisoformat(json.loads(heartbeat.read_text(encoding="utf-8"))["updated_at"])
        assert later > first
        # A live watcher satisfies the Stop guard for this session's processing card.
        assert tool_guard_hook.stop_decision(stop_payload, tmp_path) is None
        watcher.join(timeout=10.0)
        assert result["code"] == manager_callback_watch.EXIT_TIMEOUT
    finally:
        watcher.join(timeout=10.0)
    assert not heartbeat.exists()
    assert not list(heartbeat.parent.iterdir())
    # With the watcher gone the guard blocks and names the exact command.
    reason = tool_guard_hook.stop_decision(stop_payload, tmp_path)
    assert reason is not None and f"--origin-thread-id {SESSION}" in reason


def test_watch_heartbeat_is_removed_when_a_callback_is_found(db_path, tmp_path, capsys):
    _enqueue_raw(db_path, "T1")
    assert manager_callback_watch.main(_argv("3")) == manager_callback_watch.EXIT_PENDING
    assert not _heartbeat_path(tmp_path).exists()


def test_watch_heartbeat_is_removed_on_keyboard_interrupt(db_path, tmp_path, monkeypatch):
    heartbeat = _heartbeat_path(tmp_path)
    seen: list[bool] = []

    def _interrupt(seconds):
        seen.append(heartbeat.exists())
        raise KeyboardInterrupt

    monkeypatch.setattr(manager_callback_watch.time, "sleep", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager_callback_watch.main(_argv("30"))
    assert seen == [True]
    assert not heartbeat.exists()


def test_watch_survives_an_unwritable_heartbeat(db_path, tmp_path, monkeypatch, capsys):
    # A file where the heartbeat directory belongs makes every write fail.
    blocker = tmp_path.joinpath(*tool_guard_hook.MANAGER_WATCH_PARTS)
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory", encoding="utf-8")
    _enqueue_raw(db_path, "T1")
    assert manager_callback_watch.main(_argv("3")) == manager_callback_watch.EXIT_PENDING
    assert blocker.read_text(encoding="utf-8") == "not a directory"
