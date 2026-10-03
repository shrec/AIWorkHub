"""B953: callback dispatcher lifecycle honesty + auto-recovery.

The measured defect: ``dispatcher_health`` reported ``ok=true`` /
``repo_id=""`` / ``status="stopped"`` even in the extension-owned coordinator
process where a dispatcher is EXPECTED -- a silent failure with no cause, and
the canonical ``repo_id`` was clobbered to ``""`` by the stopped dispatcher's
own empty one. This locks:

* the canonical ``repo_id`` is never overwritten by a dispatcher's empty one;
* when dispatch is EXPECTED (extension window id or a verified Claude manager)
  an unregistered/stopped dispatcher, an empty/mismatched ``repo_id``, or a
  start error is a HARD, recoverable health failure (``ok=False``);
* headless / uninitialized / manager-inbox states stay non-degraded;
* ``dispatcher_ensure_started`` fails hard on an empty ``repo_id`` instead of
  starting a dispatcher that silently fails closed;
* ``dispatcher_watchdog`` re-ensures a down-but-expected dispatcher;
* terminal callbacks are durable in the outbox regardless of dispatcher state
  and replay on the next claim after recovery.
"""
from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store, core  # noqa: E402


_OLDEST = "2026-09-21T00:00:00Z"


def _readiness(ready: bool = True, repo_id: str = "repo_canon", reason: str = ""):
    return types.SimpleNamespace(ready=ready, repo_id=repo_id, reason=reason)


def _patch_core(
    monkeypatch, *, readiness, bridge_health,
    provider: str = "codex", claude_identity=None, window_id: str = "", transport: str = "",
    backlog: int = 0,
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: Path("/tmp/aiworkhub-b953-repo"))
    monkeypatch.setattr(core.task_store, "storage_readiness", lambda root: readiness)
    oldest = _OLDEST if backlog else ""
    monkeypatch.setattr(core.task_store, "callback_bridge_health",
                        lambda root: {"backlog_count": backlog, "oldest_pending_at": oldest})
    fake_bridge = types.SimpleNamespace(dispatcher_health=lambda root: dict(bridge_health))
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: fake_bridge)
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda root=None: {"selected_provider": provider})
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: claude_identity)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", window_id)
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", transport)


_UNREGISTERED = {"dispatcher_running": False, "registered": False, "repo_id": "", "last_start_error": ""}
_RUNNING = {"dispatcher_running": True, "registered": True, "repo_id": "repo_canon", "last_start_error": ""}
_STOPPED = {"dispatcher_running": False, "registered": True, "repo_id": "repo_canon", "last_start_error": ""}


# --- dispatcher_health honesty ---------------------------------------------

def test_canonical_repo_id_never_overridden_by_empty(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(repo_id="repo_canon"),
                bridge_health=_UNREGISTERED, window_id="win1")
    h = core.dispatcher_health()
    assert h["repo_id"] == "repo_canon"        # canonical truth wins
    assert h["dispatcher_repo_id"] == ""       # the dispatcher's own empty one, surfaced separately
    assert h["ok"] is False and h["healthy"] is False
    assert "dispatcher_unregistered" in h["problems"]
    assert h["recoverable"] is True


def test_headless_not_expected_stays_ok(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_UNREGISTERED, window_id="")
    h = core.dispatcher_health()
    assert h["dispatch_expected"] is False
    assert h["ok"] is True and h["healthy"] is True and h["problems"] == []


def test_expected_but_stopped_is_hard_failure(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_STOPPED, window_id="win1")
    h = core.dispatcher_health()
    assert h["ok"] is False and h["recoverable"] is True
    assert "dispatcher_stopped" in h["problems"]


def test_expected_repo_id_empty_is_hard_failure(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(repo_id=""), bridge_health=_UNREGISTERED, window_id="win1")
    h = core.dispatcher_health()
    assert h["ok"] is False
    assert "repo_id_unavailable" in h["problems"]


def test_repo_id_mismatch_is_flagged(monkeypatch):
    mismatched = {"dispatcher_running": True, "registered": True, "repo_id": "other_repo", "last_start_error": ""}
    _patch_core(monkeypatch, readiness=_readiness(repo_id="repo_canon"), bridge_health=mismatched, window_id="win1")
    h = core.dispatcher_health()
    assert h["ok"] is False
    assert "dispatcher_repo_id_mismatch" in h["problems"]


def test_running_with_matching_repo_id_is_healthy(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(repo_id="repo_canon"), bridge_health=_RUNNING, window_id="win1")
    h = core.dispatcher_health()
    assert h["ok"] is True and h["status"] == "running" and h["problems"] == []


def test_manager_inbox_healthy_without_dispatcher(monkeypatch):
    identity = {"provider": "claude", "session_id": str(uuid.uuid4()), "window_id": "claude_vscode_1"}
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_UNREGISTERED,
                provider="claude", claude_identity=identity, window_id="", transport="")
    h = core.dispatcher_health()
    assert h["status"] == "manager_inbox"
    assert h["ok"] is True and h["healthy"] is True and h["problems"] == []


def test_native_codex_manager_inbox_is_healthy_without_sideband(monkeypatch):
    _patch_core(
        monkeypatch,
        readiness=_readiness(),
        bridge_health=_UNREGISTERED,
        provider="codex",
        claude_identity=None,
        window_id="window_native_codex",
        transport="manager_inbox",
    )
    h = core.dispatcher_health()
    assert h["status"] == "manager_inbox"
    assert h["dispatch_expected"] is True
    assert h["ok"] is True and h["healthy"] is True and h["problems"] == []
    assert h["backlog_count"] == 0 and h["oldest_pending_at"] == ""


# --- NF-2026-00029: a manager inbox with waiting callbacks is not green ------

def test_claude_manager_inbox_with_backlog_reports_no_live_delivery(monkeypatch):
    identity = {"provider": "claude", "session_id": str(uuid.uuid4()), "window_id": "claude_vscode_1"}
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_UNREGISTERED,
                provider="claude", claude_identity=identity, backlog=7)
    h = core.dispatcher_health()
    assert h["status"] == "manager_inbox" and h["dispatch_expected"] is True
    assert h["ok"] is False and h["healthy"] is False
    assert h["problems"] == ["manager_inbox_no_live_delivery"]
    assert h["backlog_count"] == 7 and h["oldest_pending_at"] == _OLDEST


def test_native_codex_manager_inbox_with_backlog_reports_no_live_delivery(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_UNREGISTERED,
                window_id="window_native_codex", transport="manager_inbox", backlog=2)
    h = core.dispatcher_health()
    assert h["status"] == "manager_inbox"
    assert h["problems"] == ["manager_inbox_no_live_delivery"]
    assert h["backlog_count"] == 2


def test_manager_inbox_backlog_read_failure_is_reported(monkeypatch):
    identity = {"provider": "claude", "session_id": str(uuid.uuid4()), "window_id": "claude_vscode_1"}
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_UNREGISTERED,
                provider="claude", claude_identity=identity)

    def broken(root):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(core.task_store, "callback_bridge_health", broken)
    h = core.dispatcher_health()
    assert h["healthy"] is False
    assert h["problems"] == ["callback_backlog_unavailable:RuntimeError"]
    assert "backlog_count" not in h


def test_dispatcher_route_never_reads_backlog(monkeypatch):
    # A running dispatcher (and a headless child) keep their exact prior shape.
    for window_id, bridge_health in (("win1", _RUNNING), ("", _UNREGISTERED)):
        _patch_core(monkeypatch, readiness=_readiness(), bridge_health=bridge_health, window_id=window_id)

        def unexpected(root):
            raise AssertionError("backlog must not be read outside a manager inbox")

        monkeypatch.setattr(core.task_store, "callback_bridge_health", unexpected)
        h = core.dispatcher_health()
        assert h["ok"] is True and h["problems"] == []
        assert "backlog_count" not in h and "oldest_pending_at" not in h


def test_uninitialized_is_non_degraded(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(ready=False, repo_id="", reason="not_initialized"),
                bridge_health={}, window_id="win1")
    h = core.dispatcher_health()
    assert h["ok"] is True and h["status"] == "uninitialized" and h["healthy"] is True


# --- ensure_started: register repo identity first --------------------------

def test_ensure_started_empty_repo_id_with_window_fails_hard(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(repo_id=""), bridge_health={}, window_id="win1")
    r = core.dispatcher_ensure_started()
    assert r["ok"] is False
    assert r["status"] == "repo_id_unavailable"
    assert r["dispatcher_started"] is False
    assert r["recoverable"] is True


# --- watchdog auto-recovery -------------------------------------------------

def test_watchdog_recovers_expected_down_dispatcher(monkeypatch):
    calls = {"ensure": 0}
    healths = iter([
        {"dispatch_expected": True, "healthy": False, "status": "stopped", "problems": ["dispatcher_stopped"]},
        {"dispatch_expected": True, "healthy": True, "status": "running", "problems": []},
    ])
    monkeypatch.setattr(core, "dispatcher_health", lambda: next(healths))

    def fake_ensure():
        calls["ensure"] += 1
        return {"ok": True, "status": "started", "dispatcher_started": True}

    monkeypatch.setattr(core, "dispatcher_ensure_started", fake_ensure)
    r = core.dispatcher_watchdog()
    assert calls["ensure"] == 1
    assert r["recovered"] is True and r["ok"] is True
    assert r["problems_before"] == ["dispatcher_stopped"]


def test_watchdog_noop_when_healthy(monkeypatch):
    healths = iter([
        {"dispatch_expected": True, "healthy": True, "status": "running"},
        {"dispatch_expected": True, "healthy": True, "status": "running"},
    ])
    monkeypatch.setattr(core, "dispatcher_health", lambda: next(healths))
    called = {"ensure": False}
    monkeypatch.setattr(core, "dispatcher_ensure_started",
                        lambda: called.__setitem__("ensure", True) or {
                            "seeded_review_callback_count": 1,
                            "rebound_callback_count": 0,
                        })
    r = core.dispatcher_watchdog()
    assert r["recovered"] is False and called["ensure"] is True
    assert r["reconciled"] is True
    assert r["seeded_review_callback_count"] == 1


def test_watchdog_noop_when_not_expected(monkeypatch):
    monkeypatch.setattr(core, "dispatcher_health",
                        lambda: {"dispatch_expected": False, "healthy": False, "status": "stopped"})
    called = {"ensure": False}
    monkeypatch.setattr(core, "dispatcher_ensure_started",
                        lambda: called.__setitem__("ensure", True) or {})
    r = core.dispatcher_watchdog()
    assert r["recovered"] is False and called["ensure"] is False
    assert r["reason"] == "dispatch_not_expected"


# --- terminal enqueue is durable + replays after recovery (#2, #3) ---------

def test_terminal_enqueue_is_durable_and_replays_after_recovery(tmp_path, monkeypatch):
    # Isolate the durable-enqueue + claim-replay path from the separate
    # task-liveness supersede check (which reads the tasks table).
    monkeypatch.setattr(callback_store, "_task_still_in_matching_terminal_state", lambda *a, **k: True)
    db = tmp_path / "q.sqlite"
    conn = callback_store.open_db(db)
    callback_store.init_db(conn)
    thread = str(uuid.uuid4())
    # A terminal event enqueued with NO dispatcher running is still durable (#2).
    assert callback_store.enqueue_callback(conn, "TASK_RECOVER", thread, "review_ready", provider="codex") is True
    conn.commit()
    stats = callback_store.callback_outbox_stats(conn)
    assert stats["by_state"]["pending"] == 1
    # Recovery: a freshly-(re)started dispatcher's loop claims accumulated pending (#3).
    batch = callback_store.claim_pending_callback_batch(conn, provider="codex")
    assert batch is not None
    assert batch["origin_thread_id"] == thread
    assert [m["task_id"] for m in batch["members"]] == ["TASK_RECOVER"]
    conn.close()



def test_public_dispatcher_health_passive_context_zero_parked_callbacks(tmp_path, monkeypatch):
    import json
    import pytest
    from aiworkhub import app_server_mux, codex_manager_observation, server

    repo_id = "repo_" + "1" * 32
    thread = str(uuid.uuid4())
    mux = app_server_mux.AppServerMux(
        ["app-server"], real_executable="fixture-never-started",
        sideband_dir=tmp_path / "sideband", repo_id=repo_id)
    mux._registry_path.parent.mkdir(parents=True, mode=0o700)
    mux._server_socket = object()
    mux._ready_event.set()
    mux._observe_extension_message(json.dumps({
        "id": 1, "method": "thread/resume", "params": {"threadId": thread}}).encode())
    counters = {key: 0 for key in codex_manager_observation.COUNTERS}
    mux._observe_child_message(json.dumps({
        "method": "thread/tokenUsage/updated", "params": {
            "threadId": thread, "turnId": "fixture-turn",
            "tokenUsage": {"last": counters, "total": counters, "modelContextWindow": None}}}).encode())
    mux._observe_child_message(json.dumps({"method": "thread/status/changed", "params": {
        "threadId": thread, "status": {"type": "idle"}}}).encode())
    mux._write_registry()
    _patch_core(monkeypatch, readiness=_readiness(repo_id=repo_id),
                bridge_health={**_RUNNING, "repo_id": repo_id}, window_id="fixture-window")
    target = {"selected_provider": "codex", "extension_host_pid": mux._parent_pid,
              "window_id": "fixture-window", "targets": {"codex": {
                  "route": {"thread_id": thread}, "wake": {"supported": True, "mode": "app_server_sideband"}}}}
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda root=None: target)
    monkeypatch.setattr(core, "_read_live_window_route_records", lambda root: [
        {"repo_id": repo_id, "window_id": "fixture-window", "extension_host_pid": mux._parent_pid}])
    monkeypatch.setattr(app_server_mux, "default_sideband_dir", lambda: mux._registry_path.parent.parent)
    monkeypatch.setattr(mux, "_write_to_child", lambda *args: pytest.fail("no child RPC"))
    result = server.aiworkhub_dispatcher_health()
    assert result["ok"] is True
    assert result["manager_context"]["usage_observed"] is True
    assert result["manager_context"]["status"] == {"type": "idle"}
    assert result["manager_context"]["usage"]["modelContextWindow"] is None
    assert result["manager_context"]["usage"]["last"]["totalTokens"] == 0
    assert target["targets"]["codex"]["route"]["thread_id"] == thread
    actual_instances = app_server_mux.list_live_sideband_instances
    instance = actual_instances(app_server_mux.default_sideband_dir())[0]
    monkeypatch.setattr(app_server_mux, "list_live_sideband_instances", lambda path: [instance, instance])
    assert server.aiworkhub_dispatcher_health()["manager_context"]["usage_observed"] is False
    monkeypatch.setattr(app_server_mux, "list_live_sideband_instances", lambda path: [instance])
    from dataclasses import replace
    import time
    original = instance
    instance = replace(original, heartbeat_at=1)
    assert server.aiworkhub_dispatcher_health()["manager_context"]["usage_observed"] is False
    instance = replace(original, heartbeat_at=time.time(),
                       manager_context={**original.manager_context, "usage_observed_at": 1})
    stale = server.aiworkhub_dispatcher_health()["manager_context"]
    assert stale["usage_reason"] == "stale_observation"
    assert "usage" not in stale
    instance = replace(original, parent_pid=original.parent_pid + 1)
    assert server.aiworkhub_dispatcher_health()["manager_context"]["status_observed"] is False
    instance = original
    target["selected_provider"] = "claude"
    assert server.aiworkhub_dispatcher_health()["manager_context"]["usage_observed"] is False
