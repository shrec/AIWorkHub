"""NF-2026-01020: ``manager_callback_watch --follow`` streams one line per new batch."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from aiworkhub import callback_store, manager_callback_watch


def _load_fake_store_module():
    """Reuse the NF-2026-01892 fake-store setup without copying it."""

    path = Path(__file__).with_name("test_manager_callback_watch_nf01892.py")
    spec = importlib.util.spec_from_file_location("_nf01892_fake_store", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_store = _load_fake_store_module()
db_path = _store.db_path  # pytest fixture, re-exported for this module
SESSION = _store.SESSION
_enqueue_raw = _store._enqueue_raw
_heartbeat_path = _store._heartbeat_path


class _ManualClock:
    """The watcher's ``time`` module: ``sleep`` advances a fake ``monotonic``.

    Polls are counted, not timed, so the test never waits on wall time.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def _argv(*extra: str, timeout: str = "1", poll: str = "0.1") -> list[str]:
    return ["--repo", ".", "--provider", "claude", "--origin-thread-id", SESSION,
            "--timeout", timeout, "--poll-interval", poll, *extra]


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def _scripted_peek(monkeypatch, script: list):
    """Replace the peek with a scripted sequence; the last entry repeats."""

    calls: list[dict] = []

    def _peek(conn, **kwargs):
        calls.append(kwargs)
        return script[min(len(calls) - 1, len(script) - 1)]

    monkeypatch.setattr(callback_store, "peek_claimable_callback", _peek)
    return calls


def test_without_follow_exits_zero_on_first_batch(db_path, capsys, monkeypatch):
    clock = _ManualClock()
    monkeypatch.setattr(manager_callback_watch, "time", clock)
    _enqueue_raw(db_path, "T1")
    assert manager_callback_watch.main(_argv()) == manager_callback_watch.EXIT_PENDING
    lines = _lines(capsys)
    assert len(lines) == 1
    assert lines[0]["status"] == "callback_pending"
    assert [m["task_id"] for m in lines[0]["members"]] == ["T1"]
    assert clock.sleeps == 0


def test_follow_emits_same_pending_batch_once_and_keeps_running(db_path, tmp_path, capsys, monkeypatch):
    clock = _ManualClock()
    monkeypatch.setattr(manager_callback_watch, "time", clock)
    real_peek = callback_store.peek_claimable_callback
    peeks: list[object] = []

    def _counting_peek(conn, **kwargs):
        result = real_peek(conn, **kwargs)
        peeks.append(result)
        return result

    monkeypatch.setattr(callback_store, "peek_claimable_callback", _counting_peek)
    _enqueue_raw(db_path, "T1")

    code = manager_callback_watch.main(_argv("--follow"))

    assert code == manager_callback_watch.EXIT_TIMEOUT
    # The same batch was peeked on several polls but streamed exactly once.
    assert sum(1 for p in peeks if p is not None) >= 3
    assert clock.sleeps >= 2
    lines = _lines(capsys)
    assert [line["status"] for line in lines] == ["callback_pending", "timeout"]
    assert [m["task_id"] for m in lines[0]["members"]] == ["T1"]
    assert lines[-1] == {"status": "timeout"}
    assert not _heartbeat_path(tmp_path).exists()
    # Still only a peek: nothing was claimed, leased or acknowledged.
    conn = callback_store.open_db(db_path)
    try:
        assert conn.execute("SELECT state, batch_id FROM callback_outbox").fetchall()[0][:] == ("pending", "")
        assert conn.execute("SELECT COUNT(*) FROM callback_batches").fetchone()[0] == 0
    finally:
        conn.close()


def test_follow_emits_second_different_batch_as_second_line(db_path, tmp_path, capsys, monkeypatch):
    clock = _ManualClock()
    monkeypatch.setattr(manager_callback_watch, "time", clock)
    # First batch has no batch_id (identity falls back to canonical JSON);
    # the second carries one.
    first = {"members": [{"task_id": "T1", "request_id": "req-T1", "state": "review_ready"}]}
    second = {"batch_id": "B2", "members": [{"task_id": "T2", "request_id": "req-T2", "state": "review_ready"}]}
    calls = _scripted_peek(monkeypatch, [None, first, first, second, second, second, None])

    code = manager_callback_watch.main(_argv("--follow"))

    assert code == manager_callback_watch.EXIT_TIMEOUT
    assert len(calls) > 7
    lines = _lines(capsys)
    assert lines == [
        {"status": "callback_pending", **first},
        {"status": "callback_pending", **second},
        {"status": "timeout"},
    ]
    assert not _heartbeat_path(tmp_path).exists()


def test_follow_seen_set_is_bounded(db_path, capsys, monkeypatch):
    clock = _ManualClock()
    monkeypatch.setattr(manager_callback_watch, "time", clock)
    monkeypatch.setattr(manager_callback_watch, "FOLLOW_SEEN_LIMIT", 2)
    batches = [{"batch_id": name, "members": []} for name in ("A", "B", "C")]
    # A is evicted once B and C fill the bounded set, so its return is new again.
    _scripted_peek(monkeypatch, [batches[0], batches[1], batches[2], batches[0], None])

    assert manager_callback_watch.main(_argv("--follow")) == manager_callback_watch.EXIT_TIMEOUT
    emitted = [line.get("batch_id") for line in _lines(capsys) if line["status"] == "callback_pending"]
    assert emitted == ["A", "B", "C", "A"]


def test_follow_times_out_with_exit_three_and_removes_heartbeat(db_path, tmp_path, capsys, monkeypatch):
    clock = _ManualClock()
    monkeypatch.setattr(manager_callback_watch, "time", clock)
    heartbeat = _heartbeat_path(tmp_path)
    alive: list[bool] = []
    real_sleep = clock.sleep

    def _sleep(seconds: float) -> None:
        alive.append(heartbeat.exists())
        real_sleep(seconds)

    monkeypatch.setattr(clock, "sleep", _sleep)

    code = manager_callback_watch.main(_argv("--follow", timeout="0.5"))

    assert code == manager_callback_watch.EXIT_TIMEOUT
    assert alive and all(alive)
    assert _lines(capsys) == [{"status": "timeout"}]
    assert not heartbeat.exists()
