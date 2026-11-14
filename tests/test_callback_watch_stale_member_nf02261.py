"""NF-2026-02261: the watcher's pending scan and the claim agree on stale members.

Observed: request 88127684... of needfix-NF-2026-02246 was cancelled, the card
was retried (task_retry_terminal) and relaunched.  Its 'cancelled' outbox row
stayed pending; ``peek_claimable_callback`` reported it on every poll, so
``manager_callback_watch`` exited ``callback_pending`` within seconds forever,
while the claim (``_supersede_stale_batch_members``) treats the row as stale and
never hands it out.  Both now use one predicate, ``_outbox_member_deliverable``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aiworkhub import callback_store, manager_callback_watch

SESSION = "claude-session-nf02261"
STALE_TASK = "needfix-NF-2026-02246"
STALE_REQUEST = "88127684440747b6a5f1ff06a91382a5"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tasking" / "task_queue.sqlite"
    conn = callback_store.open_db(path)
    try:
        callback_store.init_db(conn)
    finally:
        conn.close()
    return path


def _insert_task(path, task_id, *, status, substatus, claim_epoch):
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": status,
        "worker_status": status,
        "runner": "r",
        "topic": "t",
        "origin_thread_id": SESSION,
        "coordinator_provider": "claude",
        "terminal_substatus": substatus,
        "claim_epoch": claim_epoch,
    }
    conn = callback_store.open_db(path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, "r", "t", status, status, "high", "",
                json.dumps(card, sort_keys=True), now, now, SESSION, "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_outbox(path, task_id, request_id, *, transition, episode_id, created, batch_id=""):
    conn = callback_store.open_db(path)
    try:
        cursor = conn.execute(
            "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, transition, "
            "episode_id, request_id, state, created_at, updated_at, batch_id) "
            "VALUES (?, 'claude', ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (task_id, SESSION, transition, episode_id, request_id, created, created, batch_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _insert_pending_batch(path, batch_id, created):
    conn = callback_store.open_db(path)
    try:
        conn.execute(
            "INSERT INTO callback_batches(batch_id, provider, origin_thread_id, state, "
            "created_at, updated_at, member_count) VALUES (?, 'claude', ?, 'pending', ?, ?, 1)",
            (batch_id, SESSION, created, created),
        )
        conn.commit()
    finally:
        conn.close()


def _stale_relaunched_row(path, *, created="2026-11-14T00:00:00+00:00"):
    """The cancelled launch's row, after retry_terminal + relaunch of the card."""
    _insert_task(path, STALE_TASK, status="processing", substatus="cancelled", claim_epoch=2)
    return _insert_outbox(
        path, STALE_TASK, STALE_REQUEST, transition="cancelled", episode_id="1", created=created,
    )


def _live_review_row(path, task_id="T-LIVE", *, created="2026-11-14T00:00:05+00:00"):
    _insert_task(path, task_id, status="review", substatus="review_ready", claim_epoch=1)
    return _insert_outbox(
        path, task_id, f"req-{task_id}", transition="review_ready", episode_id="1", created=created,
    )


def _peek(conn, **kwargs):
    return callback_store.peek_claimable_callback(
        conn, provider="claude", origin_thread_id=SESSION, include_seed=False, **kwargs
    )


def _claim(conn, **kwargs):
    return callback_store.claim_pending_callback_batch(
        conn, provider="claude", origin_thread_id=SESSION, **kwargs
    )


def _outbox_state(conn, outbox_id):
    return conn.execute(
        "SELECT state FROM callback_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()["state"]


def test_stale_relaunched_member_is_never_reported_by_peek(db_path):
    outbox_id = _stale_relaunched_row(db_path)
    conn = callback_store.open_db(db_path)
    try:
        # The shared selection alone still names the row: this is exactly what
        # peek reported before the fix (batch_id '' outbox_ids path).
        target = callback_store._pick_claim_target(
            conn, provider="claude", origin_thread_id=SESSION,
            now_iso=callback_store.utc_now(), max_members=10,
        )
        assert target is not None and target[0] == "" and target[2] == [outbox_id]
        row = conn.execute("SELECT * FROM callback_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        assert callback_store._outbox_member_deliverable(conn, row) is False

        assert _peek(conn) is None
        assert _outbox_state(conn, outbox_id) == "pending"  # peek wrote nothing

        # The claim agrees: it never hands the row out and retires it once.
        assert _claim(conn) is None
        assert _outbox_state(conn, outbox_id) == "superseded"
        assert _peek(conn) is None
        assert _claim(conn) is None
    finally:
        conn.close()


def test_live_member_is_still_reported_and_delivered(db_path):
    live_id = _live_review_row(db_path)
    conn = callback_store.open_db(db_path)
    try:
        peeked = _peek(conn)
        assert peeked == {
            "batch_id": "",
            "members": [{"task_id": "T-LIVE", "request_id": "req-T-LIVE", "state": "review_ready"}],
        }
        claimed = _claim(conn)
        assert claimed is not None
        assert [m["outbox_id"] for m in claimed["members"]] == [live_id]
        assert _outbox_state(conn, live_id) == "inflight"
    finally:
        conn.close()


def test_mixed_rows_peek_reports_exactly_what_the_claim_delivers(db_path):
    stale_id = _stale_relaunched_row(db_path)
    live_id = _live_review_row(db_path)
    conn = callback_store.open_db(db_path)
    try:
        peeked = _peek(conn)
        assert [m["request_id"] for m in peeked["members"]] == ["req-T-LIVE"]
        claimed = _claim(conn)
        assert [m["request_id"] for m in claimed["members"]] == ["req-T-LIVE"]
        assert _outbox_state(conn, stale_id) == "superseded"
        assert _outbox_state(conn, live_id) == "inflight"
    finally:
        conn.close()


def test_stale_rows_filling_the_member_limit_do_not_hide_a_live_one(db_path):
    stale_id = _stale_relaunched_row(db_path)
    _live_review_row(db_path)
    conn = callback_store.open_db(db_path)
    try:
        # With max_members=1 the first target is the stale row alone; the claim
        # supersedes it and moves on, and peek must look past it the same way.
        peeked = _peek(conn, max_members=1)
        assert [m["request_id"] for m in peeked["members"]] == ["req-T-LIVE"]
        claimed = _claim(conn, max_members=1)
        assert [m["request_id"] for m in claimed["members"]] == ["req-T-LIVE"]
        assert _outbox_state(conn, stale_id) == "superseded"
    finally:
        conn.close()


def test_all_stale_pending_batch_is_skipped_like_the_claim_skips_it(db_path):
    _insert_pending_batch(db_path, "batch-stale", "2026-11-14T00:00:00+00:00")
    _insert_task(db_path, STALE_TASK, status="processing", substatus="cancelled", claim_epoch=2)
    stale_id = _insert_outbox(
        db_path, STALE_TASK, STALE_REQUEST, transition="cancelled", episode_id="1",
        created="2026-11-14T00:00:00+00:00", batch_id="batch-stale",
    )
    conn = callback_store.open_db(db_path)
    try:
        assert _peek(conn) is None
        assert _claim(conn) is None
        assert _outbox_state(conn, stale_id) == "superseded"
        state = conn.execute(
            "SELECT state FROM callback_batches WHERE batch_id='batch-stale'"
        ).fetchone()["state"]
        assert state == "superseded"
        assert _peek(conn) is None
    finally:
        conn.close()


def test_watcher_sleeps_instead_of_exiting_on_a_stale_member(db_path, tmp_path, monkeypatch, capsys):
    _stale_relaunched_row(db_path)
    monkeypatch.setattr(callback_store, "resolve_db_path", lambda _repo: db_path)
    code = manager_callback_watch.main(
        [
            "--repo", str(tmp_path),
            "--provider", "claude",
            "--origin-thread-id", SESSION,
            "--timeout", "0.2",
            "--poll-interval", "0.05",
        ]
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert code == manager_callback_watch.EXIT_TIMEOUT
    assert json.loads(out[-1]) == {"status": "timeout"}
