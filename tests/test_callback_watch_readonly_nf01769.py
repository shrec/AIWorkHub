"""NF-2026-01769: claude_callback_wait watches named requests read-only when
another route owns the callback lane, instead of refusing.

Measured 2026-10-27/28: a manager on the Codex route was refused with
``callback_lane_owned_by_codex`` and polled ``agent_task_status`` (about 10 KB
a reply) while three cards ran.  Delivery stays with the owning route; only
the watch is read-only.

Run: python -m pytest -q tests/test_callback_watch_readonly_nf01769.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, callback_watch, core, task_store  # noqa: E402

_ACTIVE_CHAT = "mls-" + "b0" * 16
_CODEX = {
    "provider": "codex", "session_id": "11111111-1111-4111-8111-111111111111",
    "thread_id": "11111111-1111-4111-8111-111111111111", "window_id": "window_1",
    "route_state": "verified", "callback_supported": "true",
}
_WATCH = ["req_alpha_0001", "req_beta_0002", "req_gamma_0003"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setattr(core, "repo_root", lambda: root)
    # The callback wait/ack group lives in manager_callback_wait and resolves these seams through core at call time, so patching core is the contract (NF-2026-01863).
    monkeypatch.setattr(core, "CALLBACK_WAIT_WATCH_POLL_SECONDS", 0.05)
    monkeypatch.delenv(core.CALLBACK_WAIT_HOST_TIMEOUT_MS_ENV, raising=False)
    monkeypatch.delenv(core.CALLBACK_WAIT_MAX_SECONDS_ENV, raising=False)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: dict(_CODEX))
    return root


def _row(request_id, state, *, timestamp="2026-10-28T10:00:00+00:00", **extra):
    return {
        "request_id": request_id, "task_id": f"TASK_{request_id.upper()}", "state": state,
        "preparation_phase": None, "exit_code": None, "timestamp": timestamp, **extra,
    }


def _ledger(monkeypatch, *snapshots):
    """Serve successive ledger snapshots; the last one repeats."""
    calls = {"n": 0}

    def rows(_session_id, watch):
        index = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return [dict(row) for row in snapshots[index] if row["request_id"] in set(watch)]

    monkeypatch.setattr(core, "_claude_session_process_rows", rows)
    return calls


def _seed_outbox(task_id, provider, origin):
    now = callback_store.utc_now()
    conn = core._canonical_connect()
    try:
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO callback_outbox(task_id,provider,origin_thread_id,transition,episode_id,"
            "state,created_at,updated_at,batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, provider, origin, "review_ready", "1", "pending", now, now, ""),
        )
        conn.commit()
    finally:
        conn.close()


def _delivery_state():
    conn = core._canonical_connect()
    try:
        outbox = [
            tuple(row) for row in conn.execute(
                "SELECT task_id, provider, origin_thread_id, state, batch_id FROM callback_outbox "
                "ORDER BY outbox_id"
            )
        ]
        batches = conn.execute("SELECT COUNT(*) AS n FROM callback_batches").fetchone()["n"]
    finally:
        conn.close()
    return outbox, int(batches)


def _forbid_delivery(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("read-only watch touched callback delivery")

    for name in (
        "claim_pending_callback_batch", "acknowledge_then_claim", "acknowledge_callback_batch",
        "seed_missing_review_callbacks", "defer_batch_busy", "adopt_pending_callbacks",
        "rebind_pending_callbacks",
    ):
        monkeypatch.setattr(callback_store, name, refuse)


def test_a_codex_owned_lane_with_watch_ids_returns_compact_rows_and_never_delivers(repo, monkeypatch):
    _seed_outbox("TASK_CODEX_WAKE", "codex", _CODEX["thread_id"])
    _seed_outbox("TASK_CLAUDE_WAKE", "claude", "9ea55703-f15e-4c35-8253-4e0c96516781")
    before = _delivery_state()
    _forbid_delivery(monkeypatch)
    running = [_row(request_id, "running") for request_id in _WATCH]
    _ledger(monkeypatch, running)

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH)

    assert reply["ok"] is True
    assert reply["mode"] == "read_only_watch"
    assert reply["status"] == "timeout_no_change"
    assert reply["reason"] == "callback_lane_owned_by_codex"
    assert reply["owning_route"]["provider"] == "codex"
    assert [row["request_id"] for row in reply["requests"]] == _WATCH
    assert all(
        set(row) == {"request_id", "task_id", "state", "changed", "exit_code",
                     "risk_tier", "changed_paths_count"}
        for row in reply["requests"]
    )
    assert {row["changed"] for row in reply["requests"]} == {False}
    assert "batch_id" not in reply and "lease_id" not in reply
    assert _delivery_state() == before


@pytest.mark.live_manager_chat_seat
def test_a_manager_chat_owned_lane_also_watches_read_only(repo, monkeypatch):
    state = repo / ".aiworkhub" / "runtime" / "manager_loop"
    (state / "sessions").mkdir(parents=True, exist_ok=True)
    (state / "selected.json").write_text(json.dumps({"session_id": _ACTIVE_CHAT}), encoding="utf-8")
    (state / "sessions" / f"{_ACTIVE_CHAT}.json").write_text(json.dumps({
        "session_id": _ACTIVE_CHAT, "status": "active", "backend_id": "claude_cli",
        "model": "opus", "created_at": callback_store.utc_now(),
    }), encoding="utf-8")
    _forbid_delivery(monkeypatch)
    _ledger(monkeypatch, [_row(_WATCH[0], "running")])

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH[:1])

    assert reply["mode"] == "read_only_watch"
    assert reply["reason"] == "callback_lane_owned_by_manager_chat"
    assert reply["owning_route"]["session_id"] == _ACTIVE_CHAT


def test_a_state_change_wakes_the_read_only_watch_before_the_timeout(repo, monkeypatch):
    _forbid_delivery(monkeypatch)
    monkeypatch.setenv(core.CALLBACK_WAIT_MAX_SECONDS_ENV, "60")
    running = [_row(request_id, "running") for request_id in _WATCH]
    finished = [
        running[0],
        _row(_WATCH[1], "review_ready", timestamp="2026-10-28T10:05:00+00:00", exit_code=0,
             changed_paths=["src/a.py", "tests/test_a.py"], risk_tier="high"),
        running[2],
    ]
    calls = _ledger(monkeypatch, running, running, finished)

    started = time.monotonic()
    reply = core.claude_callback_wait(timeout_seconds=30, watch_request_ids=_WATCH)
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert calls["n"] >= 3
    assert reply["status"] == "watch_changed"
    assert reply["effective_timeout_seconds"] == 30
    rows = {row["request_id"]: row for row in reply["requests"]}
    assert rows[_WATCH[1]] == {
        "request_id": _WATCH[1], "task_id": f"TASK_{_WATCH[1].upper()}", "state": "review_ready",
        "changed": True, "exit_code": 0, "risk_tier": "high", "changed_paths_count": 2,
    }
    assert rows[_WATCH[0]]["changed"] is False and rows[_WATCH[0]]["risk_tier"] is None
    assert rows[_WATCH[2]]["changed"] is False


def test_a_review_ready_row_without_a_ledger_tier_reads_the_card(repo, monkeypatch):
    task_id = f"TASK_{_WATCH[0].upper()}"
    now = callback_store.utc_now()
    conn = core._canonical_connect()
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, objective, "
            "card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES (?, 'r', 't', 'review', 'review', 'high', 'o', ?, ?, ?, ?, '')",
            (task_id, json.dumps({"task_id": task_id, "risk_tier": "medium"}), now, now,
             _CODEX["thread_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    _ledger(
        monkeypatch,
        [_row(_WATCH[0], "running")],
        [_row(_WATCH[0], "review_ready", exit_code=0, changed_paths=["a"])],
    )

    reply = core.claude_callback_wait(timeout_seconds=5, watch_request_ids=_WATCH[:1])

    assert reply["requests"] == [{
        "request_id": _WATCH[0], "task_id": task_id, "state": "review_ready", "changed": True,
        "exit_code": 0, "risk_tier": "medium", "changed_paths_count": 1,
    }]


def test_a_watch_that_sees_no_change_times_out_within_the_cap(repo, monkeypatch):
    monkeypatch.setenv(core.CALLBACK_WAIT_MAX_SECONDS_ENV, "1")
    _ledger(monkeypatch, [_row(_WATCH[0], "running")])

    started = time.monotonic()
    reply = core.claude_callback_wait(timeout_seconds=900, watch_request_ids=_WATCH)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert reply["status"] == "timeout_no_change"
    assert reply["timeout_clamped"] is True and reply["effective_timeout_seconds"] == 1
    rows = {row["request_id"]: row for row in reply["requests"]}
    assert rows[_WATCH[1]]["state"] == "absent"
    assert {row["changed"] for row in rows.values()} == {False}


def test_the_reply_for_three_watched_requests_stays_under_two_kilobytes(repo, monkeypatch):
    long_ids = [f"{index:032x}" for index in range(3)]
    _ledger(
        monkeypatch,
        [_row(request_id, "running") for request_id in long_ids],
        [
            _row(request_id, "review_ready", timestamp="2026-10-28T11:00:00+00:00", exit_code=0,
                 changed_paths=[f"src/aiworkhub/module_{n}.py" for n in range(50)],
                 risk_tier="critical")
            for request_id in long_ids
        ],
    )

    reply = core.claude_callback_wait(timeout_seconds=5, watch_request_ids=long_ids)

    assert reply["status"] == "watch_changed"
    assert len(json.dumps(reply).encode("utf-8")) < 2048


@pytest.mark.parametrize("watch", [None, [], ["", "  "]])
def test_a_non_owner_without_watch_ids_gets_todays_refusal(repo, monkeypatch, watch):
    _forbid_delivery(monkeypatch)

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=watch)

    assert reply == {"ok": False, **core._claude_lane_refusal()}
    assert reply["reason"] == "callback_lane_owned_by_codex"
    assert "mode" not in reply


def test_an_unverified_caller_with_watch_ids_is_still_refused(repo, monkeypatch):
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    _forbid_delivery(monkeypatch)

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH)

    assert reply == {"ok": False, "reason": "verified_claude_manager_required"}


def test_the_owning_claude_route_still_claims_and_acks_with_watch_ids(repo, monkeypatch):
    monkeypatch.setattr(
        callback_store, "_task_still_in_matching_terminal_state", lambda *a, **k: True
    )
    monkeypatch.setattr(
        core, "_claude_manager_identity", lambda: {"provider": "claude", "session_id": "SESSION-A"},
    )
    _seed_outbox("T-OWNED", "claude", "SESSION-A")
    _ledger(monkeypatch, [_row(_WATCH[0], "running")])

    ready = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH[:1])

    assert ready["status"] == "callback_ready" and "mode" not in ready
    assert [member["task_id"] for member in ready["members"]] == ["T-OWNED"]
    after = core.claude_callback_wait(
        timeout_seconds=1, ack_batch_id=ready["batch_id"], ack_lease_id=ready["lease_id"],
    )
    assert after["ack"]
    assert _delivery_state()[0][0][3] == "delivered"


def test_the_timing_has_one_owner():
    assert callback_watch.callback_wait_timing("bogus")[1]["requested_timeout_seconds"] == 240
    timeout, timing = callback_watch.callback_wait_timing(0)
    assert timeout == 1 and timing["effective_timeout_seconds"] == 1


def test_an_unreadable_ledger_read_is_never_reported_as_a_change(repo, monkeypatch):
    running = [_row(request_id, "running") for request_id in _WATCH]
    calls = {"n": 0}

    def flaky(_session_id, watch):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise OSError("database is locked")
        return [dict(row) for row in running]

    monkeypatch.setattr(core, "_claude_session_process_rows", flaky)

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH)

    assert calls["n"] >= 3
    assert reply["status"] == "timeout_no_change"
    assert {(row["state"], row["changed"]) for row in reply["requests"]} == {("running", False)}


def test_a_never_readable_ledger_times_out_unchanged(repo, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("database is locked")

    monkeypatch.setattr(core, "_claude_session_process_rows", broken)

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH)

    assert reply["status"] == "timeout_no_change"
    assert {(row["state"], row["changed"]) for row in reply["requests"]} == {("absent", False)}


def test_only_a_named_owning_route_unlocks_the_read_only_watch(repo, monkeypatch):
    monkeypatch.setattr(
        core, "_claude_lane_refusal", lambda: {"reason": "callback_lane_owned_by_somebody_else"}
    )
    _forbid_delivery(monkeypatch)

    reply = core.claude_callback_wait(timeout_seconds=1, watch_request_ids=_WATCH)

    assert reply == {"ok": False, "reason": "callback_lane_owned_by_somebody_else"}
