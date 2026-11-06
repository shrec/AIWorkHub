"""NF-2026-02109: a dead review chain releases its deferred manager wake at
once, and the callback watcher's read-only peek sees seed-eligible wakes.

Measured 2026-11-06: nine review_ready cards whose quality-review chains had
already failed waited 2-5 h for a manager wake. The seed reconciliation held
each ``manager_callback_deferred`` card for DEFERRED_REVIEW_WAKE_GRACE even
though its chain could never publish manager_ready, and the background
watcher, which peeks only callback_outbox rows, never saw the seed-eligible
cards at all.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiworkhub import callback_store, manager_callback_watch, review_lifecycle
from aiworkhub.sqlite_readonly import connect_readonly

SESSION = "claude-session-nf02109"
PROVIDER = "claude"


def _db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "task_queue.sqlite"
    conn = callback_store.open_db(path)
    callback_store.init_db(conn)
    review_lifecycle.ensure_schema(conn)
    conn.commit()
    return path, conn


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


def _outbox(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str, str]]:
    return [
        (str(r["origin_thread_id"]), str(r["transition"]), str(r["state"]))
        for r in conn.execute(
            "SELECT origin_thread_id, transition, state FROM callback_outbox "
            "WHERE task_id=? AND provider=? ORDER BY outbox_id",
            (task_id, PROVIDER),
        ).fetchall()
    ]


def _seed(conn: sqlite3.Connection) -> int:
    return callback_store.seed_missing_review_callbacks(
        conn, provider=PROVIDER, origin_thread_id=SESSION
    )


def _peek_readonly(path: Path):
    conn = connect_readonly(path)
    try:
        conn.row_factory = sqlite3.Row
        return callback_store.peek_claimable_callback(
            conn, provider=PROVIDER, origin_thread_id=SESSION
        )
    finally:
        conn.close()


def _db_bytes(path: Path) -> tuple[bytes, bytes]:
    wal = path.with_name(path.name + "-wal")
    return path.read_bytes(), wal.read_bytes() if wal.exists() else b""


# --- (1) a dead chain releases the young deferral on the next claim ---------

@pytest.mark.parametrize(
    "action_states",
    [
        ["failed", "retired", "retired"],
        ["completed", "failed"],
        # Completed, but the card has no manager_ready_receipt: nothing will
        # ever announce the wake.
        ["completed", "completed"],
    ],
)
def test_dead_chain_releases_young_deferred_wake(tmp_path, action_states):
    _path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_DEAD")
        _insert_chain(conn, "T_DEAD", action_states)

        assert _seed(conn) == 1
        assert _outbox(conn, "T_DEAD") == [(SESSION, "review_ready", "pending")]
        batch = callback_store.claim_pending_callback_batch(
            conn, provider=PROVIDER, origin_thread_id=SESSION
        )
        assert [m["task_id"] for m in batch["members"]] == ["T_DEAD"]
    finally:
        conn.close()


# --- (2) a live chain still holds the young deferral -------------------------

@pytest.mark.parametrize("live_state", ["pending", "leased"])
def test_live_chain_keeps_young_deferral_held(tmp_path, live_state):
    _path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_LIVE")
        _insert_chain(conn, "T_LIVE", ["failed", live_state])

        assert _seed(conn) == 0
        assert _outbox(conn, "T_LIVE") == []
    finally:
        conn.close()


def test_only_the_newest_chain_decides(tmp_path):
    _path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_RETRY")
        _insert_chain(conn, "T_RETRY", ["failed"])
        _insert_chain(conn, "T_RETRY", ["pending"], claim_epoch="2")

        assert _seed(conn) == 0
        assert _outbox(conn, "T_RETRY") == []
    finally:
        conn.close()


# --- (3) no chain row yet keeps the existing grace ---------------------------

def test_missing_chain_keeps_grace(tmp_path):
    _path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_NOCHAIN")
        assert _seed(conn) == 0
        assert _outbox(conn, "T_NOCHAIN") == []

        grace = callback_store.DEFERRED_REVIEW_WAKE_GRACE
        _insert_deferred_review(conn, "T_NOCHAIN_OLD", age=2 * grace)
        assert _seed(conn) == 1
        assert _outbox(conn, "T_NOCHAIN") == []
        assert _outbox(conn, "T_NOCHAIN_OLD") == [(SESSION, "review_ready", "pending")]
    finally:
        conn.close()


def test_chain_without_actions_is_not_dead(tmp_path):
    _path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_EMPTY")
        _insert_chain(conn, "T_EMPTY", [])
        assert _seed(conn) == 0
        assert _outbox(conn, "T_EMPTY") == []
    finally:
        conn.close()


# --- (4) the watcher peek sees a seed-eligible row, not a held one -----------

def test_peek_reports_seed_eligible_row_without_outbox_row(tmp_path):
    path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_DEAD")
        _insert_chain(conn, "T_DEAD", ["failed", "retired"])
        _insert_deferred_review(conn, "T_HELD")
        _insert_chain(conn, "T_HELD", ["pending"])
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox").fetchone()[0] == 0
    finally:
        conn.close()

    peeked = _peek_readonly(path)
    assert peeked is not None
    assert peeked["seed_pending"] is True
    assert peeked["batch_id"] == ""
    assert peeked["members"] == [
        {"task_id": "T_DEAD", "request_id": "req-T_DEAD", "state": "review_ready"}
    ]

    # The claim seeds and returns exactly what the peek announced.
    conn = callback_store.open_db(path)
    try:
        assert _seed(conn) == 1
        batch = callback_store.claim_pending_callback_batch(
            conn, provider=PROVIDER, origin_thread_id=SESSION
        )
        assert [m["task_id"] for m in batch["members"]] == ["T_DEAD"]
    finally:
        conn.close()


def test_peek_none_for_held_row(tmp_path):
    path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_HELD")
        _insert_chain(conn, "T_HELD", ["leased"])
        _insert_deferred_review(conn, "T_NOCHAIN")
    finally:
        conn.close()

    assert _peek_readonly(path) is None


def test_peek_ignores_seed_eligible_row_of_another_provider(tmp_path):
    path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_DEAD")
        _insert_chain(conn, "T_DEAD", ["failed"])
    finally:
        conn.close()

    conn = connect_readonly(path)
    try:
        conn.row_factory = sqlite3.Row
        assert callback_store.peek_claimable_callback(
            conn, provider="codex", origin_thread_id=SESSION
        ) is None
    finally:
        conn.close()


# --- (5) the peek writes nothing -------------------------------------------

def test_peek_leaves_db_byte_identical(tmp_path):
    path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_DEAD")
        _insert_chain(conn, "T_DEAD", ["failed"])
        _insert_deferred_review(conn, "T_HELD")
        _insert_chain(conn, "T_HELD", ["pending"])
    finally:
        conn.close()

    before = _db_bytes(path)
    assert _peek_readonly(path) is not None
    assert _peek_readonly(path) is not None
    assert _db_bytes(path) == before


def test_watch_exits_zero_on_seed_eligible_row(tmp_path, monkeypatch, capsys):
    path, conn = _db(tmp_path)
    try:
        _insert_deferred_review(conn, "T_DEAD")
        _insert_chain(conn, "T_DEAD", ["failed", "retired"])
    finally:
        conn.close()
    monkeypatch.setattr(callback_store, "resolve_db_path", lambda repo: path)

    code = manager_callback_watch.main(
        ["--repo", ".", "--provider", PROVIDER, "--origin-thread-id", SESSION,
         "--timeout", "3", "--poll-interval", "0.1"]
    )

    assert code == manager_callback_watch.EXIT_PENDING
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "callback_pending"
    assert payload["seed_pending"] is True
    assert [m["task_id"] for m in payload["members"]] == ["T_DEAD"]
    conn = callback_store.open_db(path)
    try:
        assert _outbox(conn, "T_DEAD") == []
    finally:
        conn.close()
