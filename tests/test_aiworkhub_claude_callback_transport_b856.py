"""B856: backend half of the Claude coordinator callback transport.

Covers: capability-detected delivery mode (panel/cli_resume/unavailable),
CoordinatorRouteKey-scoped route resolution (no cross-repository fallback),
idempotent exactly-once delivery, ack-before-delivered ordering, busy-park
(never a parallel injected turn, never dead-lettered), unavailable-stays-
pending (never lost), callback-prompt field safety, status/health
redaction (never the raw session id), and the full
review_ready -> durable outbox -> Claude CLI transport -> matching ack ->
delivered end-to-end flow, all through the SAME durable outbox machinery
used by the existing Codex path (never a second callback database).
"""
from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store  # noqa: E402
from aiworkhub.callback_bridge import (  # noqa: E402
    BusyThreadError,
    CallbackBridge,
    ClaudeCallbackAdapter,
    ClaudeCliBusyError,
    ClaudeCliProtocolError,
    ClaudeCliResumeClient,
    ClaudeCliUnavailableError,
    build_callback_prompt,
    build_claude_resume_command,
)
from aiworkhub.route_identity import CoordinatorRouteKey  # noqa: E402


def _route(**overrides) -> CoordinatorRouteKey:
    defaults = dict(
        repo_id="repo_b856_0000000000000000000000001",
        provider="claude",
        window_id="window_b856",
        session_id=str(uuid.uuid4()),
        task_id="TASK_CLAUDE_B856",
        event_id="event.b856",
    )
    defaults.update(overrides)
    return CoordinatorRouteKey(**defaults)


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# 1. Mode detection: panel / cli_resume / unavailable, honestly labeled
# ---------------------------------------------------------------------------

def test_mode_is_unavailable_with_no_transport_configured():
    adapter = ClaudeCallbackAdapter()
    assert adapter.mode == "unavailable"


def test_mode_is_panel_when_wake_transport_supplied():
    adapter = ClaudeCallbackAdapter(wake_transport=lambda **_: {"delivered": True})
    assert adapter.mode == "panel"


def test_mode_is_cli_resume_when_cli_client_configured():
    adapter = ClaudeCallbackAdapter(cli_client=ClaudeCliResumeClient())
    assert adapter.mode == "cli_resume"


def test_panel_takes_priority_over_cli_resume_when_both_configured():
    adapter = ClaudeCallbackAdapter(
        wake_transport=lambda **_: {"delivered": True},
        cli_client=ClaudeCliResumeClient(),
    )
    assert adapter.mode == "panel"


def test_unavailable_mode_never_reports_panel_delivered():
    """Capability-negative: no panel API configured -> never fakes a
    panel-delivered outcome."""
    adapter = ClaudeCallbackAdapter()
    result = adapter.deliver_or_defer(_route(), payload={"state": "review_ready"})
    assert result.delivered is False
    assert result.durable is True
    assert result.action != "delivered"
    assert "panel" not in result.action


# ---------------------------------------------------------------------------
# 2. Route resolution: CoordinatorRouteKey fails closed, no cross-repo
# ---------------------------------------------------------------------------

def test_claude_route_requires_session_id_fails_closed():
    with pytest.raises(ValueError):
        CoordinatorRouteKey(
            repo_id="repo_b856", provider="claude", window_id="w", task_id="T1",
        )


def test_claude_route_never_accepts_cross_repository_identity_reuse():
    route_a = _route(repo_id="repo_alpha_b856_0000000000000001")
    route_b = _route(repo_id="repo_beta_b856_00000000000000001", session_id=route_a.session_id)
    assert route_a.digest() != route_b.digest()
    assert route_a.canonical() != route_b.canonical()


def test_adapter_rejects_non_claude_route():
    codex_route = CoordinatorRouteKey(
        repo_id="repo_b856", provider="codex", window_id="w",
        task_id="T1", thread_id=str(uuid.uuid4()),
    )
    with pytest.raises(ValueError):
        ClaudeCallbackAdapter().deliver_or_defer(codex_route)


# ---------------------------------------------------------------------------
# 3 / ack-before-delivered: genuine matching ack required, never mark-then-hope
# ---------------------------------------------------------------------------

def test_cli_client_requires_matching_ack_before_delivered():
    def run_fn(cmd, prompt, timeout):
        # Echoes back the WRONG event_id -- must not be accepted as delivered.
        return _FakeCompleted(0, json.dumps({"ok": True, "event_id": "wrong", "request_id": ""}))

    client = ClaudeCliResumeClient(run_fn=run_fn)
    with pytest.raises(ClaudeCliProtocolError):
        client.deliver_callback(str(uuid.uuid4()), "TASK_1", "review_ready", event_id="expected", request_id="")


def test_cli_client_delivers_on_genuine_matching_ack():
    def run_fn(cmd, prompt, timeout):
        return _FakeCompleted(0, json.dumps({"ok": True, "event_id": "e1", "request_id": "r1"}))

    client = ClaudeCliResumeClient(run_fn=run_fn)
    envelope = client.deliver_callback(str(uuid.uuid4()), "TASK_1", "review_ready", event_id="e1", request_id="r1")
    assert envelope["ok"] is True


def test_adapter_never_marks_delivered_without_calling_cli_transport():
    """Ordering proof: deliver_or_defer only returns delivered=True after
    the underlying transport call itself returned (never speculative)."""
    calls: list[str] = []

    class _Recording:
        def deliver_callback_batch(self, session_id, members, *, client_user_message_id=None):
            calls.append(session_id)
            return {"ok": True}

    adapter = ClaudeCallbackAdapter(cli_client=_Recording())
    assert calls == []
    result = adapter.deliver_or_defer(_route(), payload={"state": "review_ready"})
    assert calls == [result.route.session_id]
    assert result.delivered is True


# ---------------------------------------------------------------------------
# 4. Busy-park: durable, never a parallel injected turn, never dead-lettered
# ---------------------------------------------------------------------------

def test_cli_client_detects_busy_session_as_busy_error():
    def run_fn(cmd, prompt, timeout):
        return _FakeCompleted(1, "", "session is busy with another turn")

    client = ClaudeCliResumeClient(run_fn=run_fn)
    with pytest.raises(ClaudeCliBusyError):
        client.deliver_callback(str(uuid.uuid4()), "TASK_1", "review_ready")


def test_adapter_busy_defers_durably_without_raising_by_default():
    class _Busy:
        def deliver_callback_batch(self, *args, **kwargs):
            raise ClaudeCliBusyError("busy")

    adapter = ClaudeCallbackAdapter(cli_client=_Busy())
    result = adapter.deliver_or_defer(_route(), payload={"state": "review_ready"})
    assert result.delivered is False
    assert result.durable is True
    assert "busy" in result.reason


def test_adapter_busy_raises_claude_cli_busy_error_subclass_of_busy_thread_error_when_asked():
    """This IS the hook _process_batch relies on to park (never dead-letter,
    never a second parallel turn) -- reusing the exact same BusyThreadError
    catch already proven for Codex."""
    class _Busy:
        def deliver_callback_batch(self, *args, **kwargs):
            raise ClaudeCliBusyError("busy")

    adapter = ClaudeCallbackAdapter(cli_client=_Busy())
    with pytest.raises(BusyThreadError):
        adapter.deliver_or_defer(_route(), payload={"state": "review_ready"}, raise_on_defer=True)


# ---------------------------------------------------------------------------
# 8. Unavailable stays pending -- never lost, never dead-lettered
# ---------------------------------------------------------------------------

def test_unavailable_cli_executable_stays_pending_not_dead_lettered():
    client = ClaudeCliResumeClient(executable="claude-cli-definitely-not-installed-b856")
    with pytest.raises(ClaudeCliUnavailableError):
        client.deliver_callback(str(uuid.uuid4()), "TASK_1", "review_ready")


def test_claude_cli_unavailable_error_is_a_busy_thread_error_subclass():
    """Structural proof it durably parks instead of consuming the
    dead-letter budget (mirrors ClaudeCliBusyError / SidebandThreadBusyError)."""
    assert issubclass(ClaudeCliUnavailableError, BusyThreadError)
    assert issubclass(ClaudeCliBusyError, BusyThreadError)


def test_adapter_no_transport_configured_never_dead_letters_stays_pending():
    adapter = ClaudeCallbackAdapter()
    with pytest.raises(BusyThreadError):
        adapter.deliver_or_defer(_route(), payload={"state": "review_ready"}, raise_on_defer=True)


# ---------------------------------------------------------------------------
# Prompt field-safety: only task_id/state/event_id/request_id ever reach Claude
# ---------------------------------------------------------------------------

def test_build_claude_resume_command_is_pure_and_injectable():
    cmd = build_claude_resume_command("claude", resume_session_id="sess-1")
    assert cmd[0] == "claude"
    assert "--resume" in cmd
    assert "sess-1" in cmd
    with pytest.raises(ValueError):
        build_claude_resume_command("claude", resume_session_id="")


def test_claude_cli_client_prompt_still_goes_through_the_same_safe_builder():
    """No worker output / arbitrary card text parameter exists on the
    Claude CLI delivery path either -- same signature discipline as Codex's
    build_callback_prompt (see test_callback_prompt_never_interpolates_worker_output)."""
    import inspect

    sig = inspect.signature(build_callback_prompt)
    assert set(sig.parameters) == {"task_id", "state", "event_id", "request_id"}

    deliver_sig = inspect.signature(ClaudeCliResumeClient.deliver_callback)
    allowed = {"self", "session_id", "task_id", "state", "event_id", "request_id"}
    assert set(deliver_sig.parameters) == allowed


# ---------------------------------------------------------------------------
# Status/health redaction: never the raw session id
# ---------------------------------------------------------------------------

def _seed_review_task(conn, task_id: str, session_id: str) -> None:
    """Package-local equivalent of ``AITools/taskdb.py::upsert_card`` for
    this test only: insert one 'review' task row bound to ``session_id``,
    then enqueue its terminal 'review_ready' outbox wake -- exactly what a
    real upsert_card(status='review', origin_thread_id=...) does, ported
    without pulling in the rest of that module's card-normalization
    surface (out of scope for CallbackBridge, which never calls it)."""
    now = callback_store.utc_now()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "runner": "r",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "b856 claude callback e2e",
        "origin_thread_id": session_id,
        "claim_epoch": 0,
    }
    conn.execute(
        """
        INSERT INTO tasks (
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, "r", "task_mcp", "review", "review", "high", card["objective"],
         json.dumps(card, ensure_ascii=False, sort_keys=True), now, now, session_id),
    )
    conn.commit()
    callback_store.enqueue_callback(conn, task_id, session_id, "review_ready", episode_id="0")


def test_claude_status_never_exposes_raw_session_id():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = callback_store.open_db(db_path)
        callback_store.init_db(conn)
        session_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B856_STATUS", session_id)
        conn.close()

        def run_fn(cmd, prompt, timeout):
            return _FakeCompleted(0, json.dumps({"ok": True}))

        bridge = CallbackBridge(
            repo=repo, db_path=db_path, state_path=repo / "state.json",
            transport="claude_cli", claude_repo_id="repo_b856", claude_window_id="window_b856",
            claude_cli_run_fn=run_fn, lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
        )
        bridge.run_once()
        status = bridge.claude_status()
        assert session_id not in json.dumps(status)
        for field in ("mode", "session_suffix", "pending_count", "health"):
            assert field in status


def test_claude_cli_transport_requires_repo_and_window_identity_fail_closed():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError):
            CallbackBridge(repo=Path(d), db_path=Path(d) / "q.sqlite", transport="claude_cli")


# ---------------------------------------------------------------------------
# 7. End-to-end: review_ready -> durable outbox -> Claude transport -> ack -> delivered
# ---------------------------------------------------------------------------

def test_e2e_review_ready_outbox_claude_transport_ack_delivered():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = callback_store.open_db(db_path)
        callback_store.init_db(conn)
        session_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B856_CLAUDE", session_id)
        conn.close()

        calls: list[list[str]] = []

        def run_fn(cmd, prompt, timeout):
            calls.append(cmd)
            envelope = {"ok": True}
            # single-member batch: adapter/client require event_id/request_id
            # echo -- our seeded task had none, so echo empty strings back.
            envelope["event_id"] = ""
            envelope["request_id"] = ""
            return _FakeCompleted(0, json.dumps(envelope))

        bridge = CallbackBridge(
            repo=repo, db_path=db_path, state_path=repo / "state.json",
            transport="claude_cli", claude_repo_id="repo_b856_e2e", claude_window_id="window_b856_e2e",
            claude_cli_run_fn=run_fn, lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
        )
        result = bridge.run_once()
        assert result["ok"] is True
        assert result["action"] == "delivered"
        assert session_id not in json.dumps(result)
        assert len(calls) == 1
        assert "--resume" in calls[0]
        assert session_id in calls[0]

        stats = callback_store.callback_outbox_stats(callback_store.open_db(db_path))
        assert stats["by_state"]["delivered"] == 1
        assert stats["by_state"]["pending"] == 0

        # exactly-once: a second run finds nothing left.
        assert bridge.run_once() == {"ok": True, "action": "no_pending"}


def test_e2e_busy_session_parks_durably_never_dead_letters():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = callback_store.open_db(db_path)
        callback_store.init_db(conn)
        session_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B856_BUSY", session_id)
        conn.close()

        def run_fn(cmd, prompt, timeout):
            return _FakeCompleted(1, "", "claude session is busy")

        bridge = CallbackBridge(
            repo=repo, db_path=db_path, state_path=repo / "state.json",
            transport="claude_cli", claude_repo_id="repo_b856_busy", claude_window_id="window_b856_busy",
            claude_cli_run_fn=run_fn, lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
        )
        result = bridge.run_once()
        assert result["ok"] is False

        stats = callback_store.callback_outbox_stats(callback_store.open_db(db_path))
        assert stats["by_state"]["pending"] == 1
        assert stats["by_state"]["dead_letter"] == 0

        status = bridge.claude_status()
        assert session_id not in json.dumps(status)
        assert status["pending_count"] == 1


def test_e2e_unavailable_cli_stays_pending_not_lost():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = callback_store.open_db(db_path)
        callback_store.init_db(conn)
        session_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B856_UNAVAILABLE", session_id)
        conn.close()

        bridge = CallbackBridge(
            repo=repo, db_path=db_path, state_path=repo / "state.json",
            transport="claude_cli", claude_repo_id="repo_b856_unavail", claude_window_id="window_b856_unavail",
            claude_executable="claude-cli-definitely-not-installed-b856",
            lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
        )
        result = bridge.run_once()
        assert result["ok"] is False

        stats = callback_store.callback_outbox_stats(callback_store.open_db(db_path))
        assert stats["by_state"]["pending"] == 1
        assert stats["by_state"]["dead_letter"] == 0
