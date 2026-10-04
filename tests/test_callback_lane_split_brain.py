"""NF-2026-00843 / NF-2026-00972: one callback lane per wake, owned by the verified manager.

Measured in EntryLink on 2026-10-04: 275 pending manager_chat rows for ONE
(task, blocked, episode 7) wake. The Codex dispatcher's seed looked for the
row under ``codex`` while enqueue_callback files an ``mls-`` origin under
``manager_chat``; the Manager Chat rebind then moved each new copy off the
closed session, so the next seed found nothing and enqueued it again.

Run: python -m pytest -q tests/test_callback_lane_split_brain.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, core, task_store  # noqa: E402

_CLOSED_CHAT = "mls-" + "3f" * 16
_ACTIVE_CHAT = "mls-" + "b0" * 16
_CODEX_THREAD = "11111111-1111-4111-8111-111111111111"
_CLAUDE_SESSION = "9ea55703-f15e-4c35-8253-4e0c96516781"


def _make_db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _review_task(
    conn, task_id: str, origin: str, *, provider: str = "codex", status: str = "review",
    archived_at: str = "", episode: int = 1,
) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "status": status,
        "worker_status": status,
        "coordinator_provider": provider,
        "origin_thread_id": origin,
        "claim_epoch": episode,
        "terminal_substatus": "review_ready" if status == "review" else "",
    }
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, objective, "
        "card_json, created_at, updated_at, origin_thread_id, archived_at) "
        "VALUES (?, 'r', 'task_mcp', ?, ?, 'high', 'o', ?, ?, ?, ?, ?)",
        (task_id, status, status, json.dumps(card), now, now, origin, archived_at),
    )
    conn.commit()


def _rows(conn, task_id: str) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            "SELECT outbox_id, provider, origin_thread_id, transition, episode_id, state, "
            "last_error, recovery_count, request_id FROM callback_outbox WHERE task_id=? "
            "ORDER BY outbox_id",
            (task_id,),
        )
    ]


def _pending(conn, task_id: str) -> list[dict]:
    return [row for row in _rows(conn, task_id) if row["state"] == "pending"]


def _insert_row(conn, task_id, provider, origin, state, *, episode="1", request_id=""):
    now = callback_store.utc_now()
    conn.execute(
        "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, transition, episode_id, "
        "request_id, state, created_at, updated_at) VALUES (?, ?, ?, 'review_ready', ?, ?, ?, ?, ?)",
        (task_id, provider, origin, episode, request_id, state, now, now),
    )
    conn.commit()


# --- the measured duplicate loop --------------------------------------------


def test_the_dispatcher_seed_does_not_re_enqueue_a_wake_the_chat_lane_moved(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_LOOP", _CLOSED_CHAT)

    for _ in range(5):  # dispatcher pass, then the Manager Chat wake's rebind
        callback_store.seed_missing_review_callbacks(conn, provider="codex")
        callback_store.rebind_pending_callbacks(
            conn, provider="manager_chat", origin_thread_id=_ACTIVE_CHAT,
        )

    pending = _pending(conn, "TASK_LOOP")
    assert [(row["provider"], row["origin_thread_id"]) for row in pending] == [
        ("manager_chat", _ACTIVE_CHAT)
    ]


def test_a_rebind_collapses_copies_of_one_wake_already_on_the_route(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_COPIES", _CLOSED_CHAT)
    for _ in range(3):
        _insert_row(conn, "TASK_COPIES", "manager_chat", _ACTIVE_CHAT, "pending")

    callback_store.rebind_pending_callbacks(
        conn, provider="manager_chat", origin_thread_id=_ACTIVE_CHAT,
    )

    rows = _rows(conn, "TASK_COPIES")
    assert [row["state"] for row in rows] == ["pending", "superseded", "superseded"]
    assert {row["last_error"] for row in rows[1:]} == {callback_store.ROUTE_DUPLICATE_REASON}


def test_a_rebind_never_hands_a_route_a_second_copy_of_a_wake_it_already_received(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_DELIVERED", _CODEX_THREAD)
    _insert_row(conn, "TASK_DELIVERED", "codex", _CODEX_THREAD, "delivered")
    _insert_row(conn, "TASK_DELIVERED", "codex", "22222222-2222-4222-8222-222222222222", "pending")

    callback_store.rebind_pending_callbacks(conn, provider="codex", origin_thread_id=_CODEX_THREAD)

    assert _pending(conn, "TASK_DELIVERED") == []
    assert [row["state"] for row in _rows(conn, "TASK_DELIVERED")] == ["delivered", "superseded"]


def test_outbox_stats_partition_pending_rows_by_provider(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_STATS", _CLOSED_CHAT)
    for _ in range(3):
        _insert_row(conn, "TASK_STATS", "manager_chat", _ACTIVE_CHAT, "pending")
    _insert_row(conn, "TASK_STATS", "codex", _CODEX_THREAD, "pending")

    partitions = callback_store.callback_outbox_stats(conn)["pending_by_provider"]

    assert partitions["manager_chat"]["count"] == 3
    assert partitions["manager_chat"]["duplicate_rows"] == 2
    assert partitions["manager_chat"]["younger_than_1h"] == 3
    assert partitions["manager_chat"]["older_than_24h"] == 0
    assert partitions["codex"]["count"] == 1
    assert partitions["codex"]["duplicate_rows"] == 0


# --- cross-provider adoption -------------------------------------------------


def test_a_verified_claude_manager_adopts_a_codex_wake_exactly_once(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_ADOPT", _CODEX_THREAD)
    _insert_row(conn, "TASK_ADOPT", "codex", _CODEX_THREAD, "pending", request_id="req_1")

    first = callback_store.adopt_pending_callbacks(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )
    second = callback_store.adopt_pending_callbacks(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )
    # Neither the old provider's dispatcher nor a verified-route seed revives it.
    callback_store.seed_missing_review_callbacks(conn, provider="codex")
    callback_store.seed_missing_review_callbacks(
        conn, provider="codex", origin_thread_id=_CODEX_THREAD,
    )

    assert first["adopted"] == 1 and second["adopted"] == 0
    original, adopted = _rows(conn, "TASK_ADOPT")
    assert (original["provider"], original["origin_thread_id"], original["state"]) == (
        "codex", _CODEX_THREAD, "superseded",
    )
    assert original["last_error"] == "adopted_by_claude"
    assert (adopted["provider"], adopted["origin_thread_id"], adopted["state"]) == (
        "claude", _CLAUDE_SESSION, "pending",
    )
    assert (adopted["transition"], adopted["episode_id"], adopted["request_id"]) == (
        "review_ready", "1", "req_1",
    )
    events = [
        json.loads(row["payload_json"]) for row in conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event='callback_adopted'",
            ("TASK_ADOPT",),
        )
    ]
    assert events == [{
        "transition": "review_ready", "episode_id": "1", "outcome": "adopted",
        "from_provider": "codex", "from_origin_thread_id": _CODEX_THREAD, "to_provider": "claude",
    }]
    batch = callback_store.claim_pending_callback_batch(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )
    assert [m["task_id"] for m in batch["members"]] == ["TASK_ADOPT"]
    assert callback_store.claim_pending_callback_batch(conn, provider="codex") is None


@pytest.mark.parametrize("state", ["inflight", "delivered", "dead_letter"])
def test_adoption_never_takes_a_row_that_is_not_pending(tmp_path, state):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_KEEP", _CODEX_THREAD)
    _insert_row(conn, "TASK_KEEP", "codex", _CODEX_THREAD, state)

    result = callback_store.adopt_pending_callbacks(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )

    assert result["adopted"] == 0
    assert [(row["provider"], row["state"]) for row in _rows(conn, "TASK_KEEP")] == [("codex", state)]


def test_adoption_retires_a_wake_the_adopting_route_already_holds(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_HELD", _CODEX_THREAD)
    _insert_row(conn, "TASK_HELD", "claude", _CLAUDE_SESSION, "delivered")
    _insert_row(conn, "TASK_HELD", "codex", _CODEX_THREAD, "pending")

    result = callback_store.adopt_pending_callbacks(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )

    assert result == {"scanned": 1, "adopted": 0, "duplicates_superseded": 1}
    assert [row["state"] for row in _rows(conn, "TASK_HELD")] == ["delivered", "superseded"]


def test_adoption_leaves_stale_and_manager_chat_rows_alone(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_DONE", _CODEX_THREAD, status="finished")
    _insert_row(conn, "TASK_DONE", "codex", _CODEX_THREAD, "pending")
    _review_task(conn, "TASK_CHAT", _ACTIVE_CHAT)
    _insert_row(conn, "TASK_CHAT", "manager_chat", _ACTIVE_CHAT, "pending")

    result = callback_store.adopt_pending_callbacks(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )

    assert result["adopted"] == 0
    assert [row["provider"] for row in _pending(conn, "TASK_DONE")] == ["codex"]
    assert [row["provider"] for row in _pending(conn, "TASK_CHAT")] == ["manager_chat"]


@pytest.mark.parametrize("provider, origin", [("manager_chat", _ACTIVE_CHAT), ("claude", "")])
def test_adoption_requires_a_real_manager_route(tmp_path, provider, origin):
    conn = _make_db(tmp_path)
    with pytest.raises(ValueError):
        callback_store.adopt_pending_callbacks(conn, provider=provider, origin_thread_id=origin)


# --- archived / superseded cards never replay --------------------------------


def test_prune_supersedes_the_pending_wake_of_an_archived_card(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_ARCHIVED", _CLAUDE_SESSION, archived_at=callback_store.utc_now())
    _insert_row(conn, "TASK_ARCHIVED", "claude", _CLAUDE_SESSION, "pending")

    result = callback_store.prune_stale_pending_callbacks(conn)

    assert result["pruned"] == 1
    assert [row["state"] for row in _rows(conn, "TASK_ARCHIVED")] == ["superseded"]


@pytest.mark.parametrize("archived_at, status", [("2026-10-04T21:14:00+00:00", "review"), ("", "superseded")])
def test_the_claim_path_supersedes_an_archived_or_superseded_card_wake(tmp_path, archived_at, status):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_GONE", _CLAUDE_SESSION, status=status, archived_at=archived_at)
    _insert_row(conn, "TASK_GONE", "claude", _CLAUDE_SESSION, "pending")

    batch = callback_store.claim_pending_callback_batch(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )

    assert batch is None
    assert [row["state"] for row in _rows(conn, "TASK_GONE")] == ["superseded"]


# --- bootstrap and the Claude ack lane agree ----------------------------------


def _select_chat(root: Path, session_id: str = _ACTIVE_CHAT) -> None:
    state = root / ".aiworkhub" / "runtime" / "manager_loop"
    (state / "sessions").mkdir(parents=True, exist_ok=True)
    (state / "selected.json").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    (state / "sessions" / f"{session_id}.json").write_text(json.dumps({
        "session_id": session_id, "status": "active", "backend_id": "claude_cli",
        "model": "opus", "created_at": callback_store.utc_now(),
    }), encoding="utf-8")


_PENDING_CODEX = {
    "provider": "codex", "session_id": "episode_pending", "thread_id": "",
    "window_id": "window_1", "route_state": "route_pending", "callback_supported": "false",
}
_CLAUDE = {"provider": "claude", "session_id": _CLAUDE_SESSION, "window_id": "claude_vscode_1"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    monkeypatch.setattr(core, "_bootstrap_dispatcher", lambda *_a: {"ensured": False})
    monkeypatch.setattr(core, "_schedule_task_hygiene", lambda **_k: {})
    return root


def test_bootstrap_keeps_a_verified_claude_route_while_a_manager_chat_is_open(repo, monkeypatch):
    _select_chat(repo)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE))
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)

    reply = core.manager_bootstrap()

    assert reply["provider"] == "claude"
    assert reply["manager_route"]["session_id"] == _CLAUDE_SESSION
    assert reply["manager_chat_seat"]["session_id"] == _ACTIVE_CHAT


@pytest.mark.parametrize("call", [
    lambda: core.claude_callback_wait(timeout_seconds=1),
    lambda: core.claude_callback_ack("batch", "lease"),
    lambda: core.claude_callback_ack_by_reference(task_id="TASK"),
])
def test_the_claude_lane_names_the_manager_chat_seat_that_owns_the_wakes(repo, monkeypatch, call):
    _select_chat(repo)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: dict(_PENDING_CODEX))

    assert core.manager_bootstrap()["manager_verified"] is True
    reply = call()

    assert reply["reason"] == "callback_lane_owned_by_manager_chat"
    assert reply["owning_route"]["session_id"] == _ACTIVE_CHAT


def test_the_claude_lane_names_the_codex_route_and_refuses_only_the_unverified(repo, monkeypatch):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: dict(_PENDING_CODEX))

    owned = core.claude_callback_ack("batch", "lease")
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    unverified = core.claude_callback_ack("batch", "lease")

    assert owned["reason"] == "callback_lane_owned_by_codex"
    assert owned["owning_route"]["route_state"] == "route_pending"
    assert unverified["reason"] == "verified_claude_manager_required"


def test_a_verified_manager_bootstrap_adopts_pending_codex_wakes(repo, monkeypatch):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE))
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    conn = core._canonical_connect()
    try:
        _review_task(conn, "TASK_BOOT", _CODEX_THREAD)
        _insert_row(conn, "TASK_BOOT", "codex", _CODEX_THREAD, "pending")
    finally:
        conn.close()

    gate = core.manager_bootstrap()
    reply = core.manager_bootstrap(bootstrap_call=True)

    assert "callback_adoption" not in gate
    assert reply["callback_adoption"]["adopted"] == 1


@pytest.mark.parametrize("codex", [
    _PENDING_CODEX,
    {**_PENDING_CODEX, "thread_id": _CODEX_THREAD, "session_id": _CODEX_THREAD},
])
def test_a_pending_route_bootstrap_adopts_nothing(repo, monkeypatch, codex):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: dict(codex))

    reply = core.manager_bootstrap(bootstrap_call=True)

    assert reply["callback_adoption"] == {
        "adopted": 0, "reason": "callback_capable_manager_route_required",
    }
