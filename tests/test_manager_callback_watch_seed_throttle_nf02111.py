"""NF-2026-02111: throttle the seed-eligible scan to once per interval."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiworkhub import callback_store, manager_callback_watch, review_lifecycle
from aiworkhub.sqlite_readonly import connect_readonly

SESSION = "sessA"
PROVIDER = "claude"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = callback_store.open_db(path)
    callback_store.init_db(conn)
    review_lifecycle.ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


def _insert_deferred_review(
    conn: sqlite3.Connection, task_id: str, *, age: timedelta = timedelta(minutes=1)
) -> None:
    """A review_ready card whose manager wake was deferred to its review chain."""
    now = callback_store.utc_now()
    request_id = f"req-{task_id}"
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "runner": "r",
        "topic": "task_mcp",
        "origin_thread_id": SESSION,
        "coordinator_provider": PROVIDER,
        "terminal_substatus": "review_ready",
        "claim_epoch": 1,
        "launch_request_id": request_id,
        "terminal_review": {
            "substatus": "review_ready",
            "recorded_at": (datetime.now(timezone.utc) - age).isoformat(),
            "evidence": {
                "request_id": request_id,
                "manager_callback_deferred": {"reason": "system_quality_review_pending"},
            },
        },
    }
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
        "objective, card_json, created_at, updated_at, origin_thread_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, "r", "task_mcp", "review", "review", "high", "",
            json.dumps(card, ensure_ascii=False, sort_keys=True), now, now, SESSION,
        ),
    )
    conn.commit()


def _insert_chain(
    conn: sqlite3.Connection, task_id: str, action_states: list[str], *, claim_epoch: str = "1"
) -> int:
    """One review chain for the card's request with actions in ``action_states``."""
    now = callback_store.utc_now()
    request_id = f"req-{task_id}"
    cur = conn.execute(
        "INSERT INTO review_chains(target_task_id, target_request_id, claim_epoch, "
        "packet_sha256, candidate_sha256, chain_identity_json, chain_identity_sha256, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (task_id, request_id, claim_epoch, "p" * 64, "c" * 64, "{}", uuid.uuid4().hex, now),
    )
    chain_id = int(cur.lastrowid)
    for index, state in enumerate(action_states):
        conn.execute(
            "INSERT INTO review_action_outbox(chain_id, action_index, phase, action_type, "
            "target_task_id, target_request_id, claim_epoch, descriptor_json, descriptor_sha256, "
            "state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chain_id, index, "review", f"action_{index}", task_id, request_id, claim_epoch,
                "{}", "d" * 64, state, now, now,
            ),
        )
    conn.commit()
    return chain_id


def test_include_seed_false_skips_seed_scan(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    conn = callback_store.open_db(db_path)
    _insert_deferred_review(conn, "T_DEAD")
    _insert_chain(conn, "T_DEAD", ["failed"])
    conn.close()

    conn = connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        calls = {"n": 0}
        real = callback_store._peek_seed_eligible_members

        def _spy(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(callback_store, "_peek_seed_eligible_members", _spy)

        result = callback_store.peek_claimable_callback(
            conn,
            provider=PROVIDER,
            origin_thread_id=SESSION,
            include_seed=False,
        )
        assert result is None
        assert calls["n"] == 0
    finally:
        conn.close()


def test_include_seed_true_returns_seed_pending(tmp_path):
    db_path = _db(tmp_path)
    conn = callback_store.open_db(db_path)
    _insert_deferred_review(conn, "T_DEAD")
    _insert_chain(conn, "T_DEAD", ["failed"])
    conn.close()

    conn = connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = callback_store.peek_claimable_callback(
            conn,
            provider=PROVIDER,
            origin_thread_id=SESSION,
            include_seed=True,
        )
        assert result is not None
        assert result.get("seed_pending") is True

        result_no_seed = callback_store.peek_claimable_callback(
            conn,
            provider=PROVIDER,
            origin_thread_id=SESSION,
            include_seed=False,
        )
        assert result_no_seed is None
    finally:
        conn.close()


def test_watcher_throttles_seed_peek_across_polls(tmp_path, monkeypatch):
    db_path = _db(tmp_path)

    clock = {"t": 1000.0}

    def fake_monotonic():
        return clock["t"]

    include_seed_calls = []

    def fake_peek(conn, *, provider, origin_thread_id, include_seed=True):
        include_seed_calls.append(include_seed)
        if len(include_seed_calls) >= 4:
            return {"batch_id": "b1", "members": []}
        return None

    monkeypatch.setattr(manager_callback_watch.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(manager_callback_watch.callback_store, "resolve_db_path", lambda repo: db_path)
    monkeypatch.setattr(manager_callback_watch.callback_store, "peek_claimable_callback", fake_peek)
    monkeypatch.setattr(manager_callback_watch, "connect_readonly", lambda path: sqlite3.connect(path))
    monkeypatch.setattr(manager_callback_watch, "_write_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(manager_callback_watch, "_remove_heartbeat", lambda *a, **k: None)

    sleep_calls = {"n": 0}

    def fake_sleep(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            clock["t"] += 2.0
        elif sleep_calls["n"] == 2:
            clock["t"] += manager_callback_watch.SEED_PEEK_INTERVAL_SECONDS
        else:
            clock["t"] += 2.0

    monkeypatch.setattr(manager_callback_watch.time, "sleep", fake_sleep)

    rc = manager_callback_watch.main(
        [
            "--repo",
            str(tmp_path),
            "--provider",
            PROVIDER,
            "--origin-thread-id",
            SESSION,
            "--timeout",
            "3600",
            "--poll-interval",
            "2",
        ]
    )

    assert rc == manager_callback_watch.EXIT_PENDING
    assert include_seed_calls[0] is True
    assert include_seed_calls[1] is False
    assert include_seed_calls[2] is True
