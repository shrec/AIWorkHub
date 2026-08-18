"""SCAN-E3FF: the manager seat must always get a working callback.

Four confirmed defects broke that standing requirement; each test below
drives the exact sequence that used to lose (or falsely report) a wake:

  ONE  (callback_store.enqueue_callback): review_ready then blocked in the
       SAME claim episode must enqueue a SECOND wake -- the dedup omitted
       ``transition`` and returned False for the blocked wake.
  #2   (callback_store.seed_missing_review_callbacks): the reconciler must
       find that same gap, not skip it for the same transition-blind reason.
  TWO  (callback_store terminal recovery): a recovered row must be re-pointed
       at the transition the task is in NOW, or the next claim supersedes it
       again and the exhausted recovery budget strands the wake forever.
  FOUR (callback_bridge): the single delivery thread must survive an
       exception that is neither BusyThreadError nor AppServerError, the
       failure must be surfaced (not swallowed) and the batch must not sit
       inflight for the whole lease; dispatcher health must not read "ok"
       while that thread is dead.

THREE (core.claude_callback_wait long-poll inbox) is fixed at the store
layer that this file exercises directly (route-scoped claiming); the
one-line core.py caller change it still needs is stated in the worker
report -- core.py is owned by another card and is not touched here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_bridge, callback_store  # noqa: E402


def _db(tmp_path: Path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _insert_task(
    conn,
    task_id: str,
    origin_thread_id: str,
    *,
    status: str = "review",
    terminal_substatus: str = "review_ready",
    claim_epoch: int = 0,
    provider: str = "codex",
) -> None:
    now = callback_store.utc_now()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": status,
        "worker_status": status,
        "runner": "r",
        "topic": "task_mcp",
        "origin_thread_id": origin_thread_id,
        "coordinator_provider": provider,
        "terminal_substatus": terminal_substatus,
        "claim_epoch": claim_epoch,
    }
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
        "objective, card_json, created_at, updated_at, origin_thread_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, "r", "task_mcp", status, status, "high", "",
            json.dumps(card, ensure_ascii=False, sort_keys=True), now, now, origin_thread_id,
        ),
    )
    conn.commit()


def _move_terminal(conn, task_id: str, *, status: str, terminal_substatus: str) -> None:
    """Move an already-inserted task to a new terminal state WITHOUT bumping
    claim_epoch -- exactly what the reconciler does when it finds a damaged
    review workspace and blocks the card in-episode."""
    row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    card = json.loads(row["card_json"])
    card["status"] = status
    card["worker_status"] = status
    card["terminal_substatus"] = terminal_substatus
    conn.execute(
        "UPDATE tasks SET status=?, worker_status=?, card_json=? WHERE task_id=?",
        (status, status, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
    )
    conn.commit()


def _transitions(conn, task_id: str) -> list[str]:
    return [
        str(r["transition"])
        for r in conn.execute(
            "SELECT transition FROM callback_outbox WHERE task_id=? ORDER BY outbox_id",
            (task_id,),
        ).fetchall()
    ]


# --- ONE: review_ready then blocked in one episode enqueues a second wake ---

def test_review_ready_then_blocked_same_episode_enqueues_second_wake(tmp_path):
    conn = _db(tmp_path)
    task_id, thread = "TASK_ONE", "thread-one"
    _insert_task(conn, task_id, thread, status="review", terminal_substatus="review_ready", claim_epoch=1)

    assert callback_store.enqueue_callback(
        conn, task_id, thread, "review_ready", provider="codex", episode_id="1",
    ) is True
    # Reconciler damages the review workspace and blocks the SAME card in the
    # SAME episode (no claim_epoch bump). This wake was previously lost: the
    # dedup omitted transition and matched the existing review_ready row.
    _move_terminal(conn, task_id, status="blocked", terminal_substatus="blocked")
    assert callback_store.enqueue_callback(
        conn, task_id, thread, "blocked", provider="codex", episode_id="1",
    ) is True

    assert _transitions(conn, task_id) == ["review_ready", "blocked"]

    # A GENUINE duplicate (same transition/episode/route) is still deduped --
    # the predicate was widened by transition only, not loosened wholesale.
    assert callback_store.enqueue_callback(
        conn, task_id, thread, "blocked", provider="codex", episode_id="1",
    ) is False
    assert _transitions(conn, task_id) == ["review_ready", "blocked"]


# --- #2: seed_missing_review_callbacks finds that same gap ------------------

def test_seed_finds_review_then_blocked_gap(tmp_path):
    conn = _db(tmp_path)
    task_id, thread = "TASK_GAP", "thread-gap"
    _insert_task(conn, task_id, thread, status="review", terminal_substatus="review_ready", claim_epoch=1)
    assert callback_store.enqueue_callback(
        conn, task_id, thread, "review_ready", provider="codex", episode_id="1",
    ) is True

    # Card moves to blocked in-episode; only the review_ready row exists.
    _move_terminal(conn, task_id, status="blocked", terminal_substatus="blocked")

    # The reconciler must SEED the missing blocked wake, not skip because a
    # (now-stale) review_ready row exists for the same task/route/episode.
    assert callback_store.seed_missing_review_callbacks(
        conn, provider="codex", origin_thread_id=thread,
    ) == 1
    assert "blocked" in set(_transitions(conn, task_id))

    # Idempotent: the blocked wake is not seeded twice.
    assert callback_store.seed_missing_review_callbacks(
        conn, provider="codex", origin_thread_id=thread,
    ) == 0


# --- TWO / #3: terminal recovery refreshes transition, then delivers -------

def test_terminal_recovery_refreshes_transition_and_delivers(tmp_path):
    conn = _db(tmp_path)
    task_id, thread = "TASK_RECOVER", "thread-recover"
    # The card is now blocked in episode 1 (moved from review in-episode).
    _insert_task(conn, task_id, thread, status="blocked", terminal_substatus="blocked", claim_epoch=1)
    # A review_ready row from earlier in the SAME episode was superseded when
    # the card moved to blocked; its single recovery budget is untouched.
    now = callback_store.utc_now()
    conn.execute(
        "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, transition, "
        "episode_id, state, created_at, updated_at, recovery_count) VALUES(?,?,?,?,?,?,?,?,?)",
        (task_id, "codex", thread, "review_ready", "1", "superseded", now, now, 0),
    )
    conn.commit()

    # Verified-route reconciliation recovers the stranded row and re-points it
    # at the transition the card is in RIGHT NOW.
    assert callback_store.seed_missing_review_callbacks(
        conn, provider="codex", origin_thread_id=thread,
    ) == 1
    row = conn.execute(
        "SELECT transition, state, recovery_count FROM callback_outbox WHERE task_id=?",
        (task_id,),
    ).fetchone()
    # Refreshed to blocked -- NOT left as review_ready (which the next claim
    # would immediately supersede, stranding the wake forever).
    assert row["transition"] == "blocked"
    assert row["state"] == "pending"
    assert row["recovery_count"] == 1

    # The recovered row is claimable and deliverable, not re-superseded.
    claim = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="codex", origin_thread_id=thread,
    )
    assert claim is not None
    assert [m["transition"] for m in claim["members"]] == ["blocked"]
    assert callback_store.mark_batch_delivered(conn, claim["batch_id"], claim["lease_id"]) is True
    delivered = conn.execute(
        "SELECT state FROM callback_outbox WHERE task_id=?", (task_id,),
    ).fetchone()
    assert delivered["state"] == "delivered"


# --- THREE: route-scoped claiming (two distinct manager session identities) -

def test_two_managers_route_scoped_claim_never_steals_other_route(tmp_path):
    """Two verified managers on the same repository/provider. A route-scoped
    claim must only ever lease its OWN route's batch -- never lease-and-park a
    batch belonging to the other manager's session.

    The store already supports the ``origin_thread_id`` route parameter this
    proves. THREE's remaining fix is in core.claude_callback_wait, which calls
    claim_pending_callback_batch WITHOUT that parameter (see worker report)."""
    conn = _db(tmp_path)
    session_a, session_b = "manager-a-session", "manager-b-session"
    _insert_task(conn, "TASK_A", session_a, status="review", terminal_substatus="review_ready", claim_epoch=0, provider="claude")
    _insert_task(conn, "TASK_B", session_b, status="review", terminal_substatus="review_ready", claim_epoch=0, provider="claude")
    assert callback_store.enqueue_callback(conn, "TASK_A", session_a, "review_ready", provider="claude", episode_id="0") is True
    assert callback_store.enqueue_callback(conn, "TASK_B", session_b, "review_ready", provider="claude", episode_id="0") is True

    claim_a = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert claim_a is not None
    assert claim_a["origin_thread_id"] == session_a
    assert [m["task_id"] for m in claim_a["members"]] == ["TASK_A"]

    # Manager B's batch was never leased/parked by A's claim -- still pending
    # and claimable on B's own route.
    assert conn.execute(
        "SELECT state FROM callback_outbox WHERE task_id='TASK_B'"
    ).fetchone()["state"] == "pending"
    claim_b = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_b,
    )
    assert claim_b is not None
    assert claim_b["origin_thread_id"] == session_b
    assert [m["task_id"] for m in claim_b["members"]] == ["TASK_B"]


# --- FOUR: delivery thread survives, surfaces, and releases the batch -------

def test_daemon_survives_non_typed_exception_and_surfaces_it(tmp_path):
    db_path = tmp_path / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    callback_store.init_db(conn)
    _insert_task(conn, "TASK_DAEMON", "thread-daemon", status="review", terminal_substatus="review_ready", claim_epoch=0, provider="codex")
    assert callback_store.enqueue_callback(
        conn, "TASK_DAEMON", "thread-daemon", "review_ready", provider="codex", episode_id="0",
    ) is True
    conn.close()

    bridge = callback_bridge.CallbackBridge(
        repo=tmp_path,
        db_path=db_path,
        state_path=tmp_path / "state.json",
        transport="subprocess",
        provider="codex",
    )

    def _boom(_batch, _client_user_message_id):
        # Neither BusyThreadError nor AppServerError -- the exact class of
        # failure that used to kill the single delivery thread.
        raise ValueError("neither busy nor app-server")

    bridge._deliver_batch_via_transport = _boom

    # Must return, not raise, even though delivery raised a bare ValueError.
    bridge.daemon(max_iterations=1)

    # The failure is surfaced in health, never swallowed.
    health = bridge.health()
    assert "ValueError" in health["last_delivery_error"]

    # The batch is not left sitting inflight for the whole lease.
    conn = callback_store.open_db(db_path)
    try:
        batch_states = [r["state"] for r in conn.execute("SELECT state FROM callback_batches").fetchall()]
        outbox_states = [r["state"] for r in conn.execute("SELECT state FROM callback_outbox").fetchall()]
    finally:
        conn.close()
    assert "inflight" not in batch_states
    assert "inflight" not in outbox_states


def _outbox_kwargs(tmp_path: Path) -> dict:
    db_path = tmp_path / "dispatcher_outbox.sqlite"
    conn = callback_store.open_db(db_path)
    callback_store.init_db(conn)
    conn.close()
    return {"db_path": db_path, "state_path": tmp_path / "dispatcher_state.json"}


def test_dispatcher_health_not_ok_while_delivery_thread_dead(tmp_path):
    dispatcher = callback_bridge.CallbackDispatcher(
        tmp_path / "repo", "codex", bridge_kwargs=_outbox_kwargs(tmp_path),
    )
    try:
        dispatcher.start()
        assert dispatcher.is_running() is True
        assert dispatcher.health()["ok"] is True

        # The single delivery thread dies (here: cleanly stopped; an unguarded
        # crash lands in the same not-running state).
        dispatcher._bridge.stop_daemon()
        dispatcher._thread.join(timeout=5)
        assert dispatcher.is_running() is False

        health = dispatcher.health()
        assert health["ok"] is False
        assert health["dispatcher_running"] is False
        assert "delivery_thread_not_running" in health["problems"]
    finally:
        dispatcher.stop()
