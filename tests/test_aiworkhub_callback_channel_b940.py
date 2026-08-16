"""B940: AIWorkHub Claude callback CHANNEL -- true push-wake parity.

Covers the wire shape and push loop of ``callback_channel.py``: the
``initialize`` handshake declares the ``claude/channel`` capability, the push
event is a well-formed ``notifications/claude/channel`` notification carrying
only safe task/transition text (never worker output), and the background pump
claims-emits-acks through injected wait/ack functions exactly like the real
``core.claude_callback_wait`` / ``claude_callback_ack`` machinery (no live
``claude`` parent required, mirroring ``ClaudeCliResumeClient``'s injectable
``run_fn``).
"""
from __future__ import annotations

import io
import re
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_channel  # noqa: E402
from aiworkhub.callback_channel import (  # noqa: E402
    CHANNEL_CAPABILITY,
    CHANNEL_NOTIFICATION_METHOD,
    CallbackChannelServer,
    build_channel_event,
    initialize_result,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


class _CaptureOut:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. initialize handshake declares the channel capability
# ---------------------------------------------------------------------------

def test_initialize_result_declares_claude_channel_capability():
    result = initialize_result("aiworkhub")
    assert result["capabilities"]["experimental"][CHANNEL_CAPABILITY] == {}
    assert result["serverInfo"]["name"] == "aiworkhub"
    assert result["serverInfo"]["version"]
    assert "channel" in result["instructions"].lower()


# ---------------------------------------------------------------------------
# 2. channel event is a well-formed, field-safe notification
# ---------------------------------------------------------------------------

def test_build_channel_event_single_member_shape_and_safety():
    event = build_channel_event(
        [{"task_id": "TASK_A", "state": "review_ready", "event_id": "e1", "request_id": "r1"}],
        batch_id="batch-1",
    )
    assert event["jsonrpc"] == "2.0"
    assert event["method"] == CHANNEL_NOTIFICATION_METHOD
    assert "id" not in event  # a notification never carries an id
    content = event["params"]["content"]
    assert "TASK_A" in content and "review_ready" in content
    # meta keys must be bounded identifiers per the channel contract
    for key in event["params"]["meta"]:
        assert _IDENTIFIER_RE.fullmatch(key), key
    assert event["params"]["meta"]["batch_id"] == "batch-1"
    assert event["params"]["meta"]["task_count"] == "1"


def test_build_channel_event_batch_lists_every_task_and_count():
    members = [
        {"task_id": "T1", "state": "review_ready"},
        {"task_id": "T2", "state": "blocked"},
    ]
    event = build_channel_event(members, batch_id="b2")
    content = event["params"]["content"]
    assert "T1" in content and "T2" in content
    assert event["params"]["meta"]["task_count"] == "2"


def test_build_channel_event_never_leaks_worker_output_fields():
    """Only task_id/state ever reach the wire -- an injected 'output' field on
    a member must never appear in the pushed content."""
    event = build_channel_event(
        [{"task_id": "T1", "state": "review_ready", "output": "SECRET-WORKER-STDOUT"}],
    )
    assert "SECRET-WORKER-STDOUT" not in event["params"]["content"]
    assert "SECRET-WORKER-STDOUT" not in str(event["params"]["meta"])


# ---------------------------------------------------------------------------
# 3. push loop: claim -> emit -> ack, via injected wait/ack (no live claude)
# ---------------------------------------------------------------------------

def test_pump_once_pushes_and_acks_a_ready_callback():
    session_id = str(uuid.uuid4())
    acks: list[tuple[str, str]] = []
    out = _CaptureOut()

    def wait_fn(*, timeout_seconds):
        return {
            "ok": True, "status": "callback_ready", "provider": "claude",
            "batch_id": "batch-1", "lease_id": "lease-1", "origin_thread_id": session_id,
            "members": [{"task_id": "TASK_A", "state": "review_ready", "event_id": "", "request_id": ""}],
        }

    def ack_fn(batch_id, lease_id):
        acks.append((batch_id, lease_id))
        return {"ok": True, "status": "delivered"}

    server = CallbackChannelServer(stdout=out, wait_fn=wait_fn, ack_fn=ack_fn)
    status = server.pump_once()

    assert status == "pushed"
    assert acks == [("batch-1", "lease-1")]  # durably acked after the push
    assert len(out.lines) == 1
    pushed = out.lines[0]
    assert CHANNEL_NOTIFICATION_METHOD in pushed
    assert "TASK_A" in pushed


def test_pump_once_idle_when_no_callback_emits_nothing():
    out = _CaptureOut()
    server = CallbackChannelServer(
        stdout=out,
        wait_fn=lambda *, timeout_seconds: {"ok": True, "status": "timeout_no_callback"},
        ack_fn=lambda *_: {"ok": True},
    )
    assert server.pump_once() == "idle"
    assert out.lines == []


def test_pump_once_no_manager_when_not_verified_claude_session():
    out = _CaptureOut()
    called = {"ack": 0}

    def ack_fn(*_):
        called["ack"] += 1
        return {"ok": True}

    server = CallbackChannelServer(
        stdout=out,
        wait_fn=lambda *, timeout_seconds: {"ok": False, "reason": "verified_claude_manager_required"},
        ack_fn=ack_fn,
    )
    assert server.pump_once() == "no_manager"
    assert out.lines == []
    assert called["ack"] == 0  # never ack when there was nothing to push


def test_pump_once_push_unacked_when_ack_rejected_stays_recoverable():
    out = _CaptureOut()

    def wait_fn(*, timeout_seconds):
        return {
            "ok": True, "status": "callback_ready", "batch_id": "b", "lease_id": "l",
            "members": [{"task_id": "T", "state": "review_ready"}],
        }

    server = CallbackChannelServer(
        stdout=out, wait_fn=wait_fn, ack_fn=lambda *_: {"ok": False, "status": "ack_rejected"},
    )
    # Still emitted (the durable row is unacked -> lease reclaim re-pushes later).
    assert server.pump_once() == "push_unacked"
    assert len(out.lines) == 1


# ---------------------------------------------------------------------------
# 4. stdio handshake integration
# ---------------------------------------------------------------------------

def test_handle_message_initialize_returns_capability_and_marks_initialized():
    server = CallbackChannelServer(
        wait_fn=lambda *, timeout_seconds: {"ok": True, "status": "timeout_no_callback"},
        ack_fn=lambda *_: {"ok": True},
        route_fn=lambda: {"provider": "claude"},  # pin the Claude seat: initialize starts the pump
    )
    try:
        response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert response["id"] == 1
        assert response["result"]["capabilities"]["experimental"][CHANNEL_CAPABILITY] == {}
        assert server._initialized.is_set()
    finally:
        # ``initialize`` starts the background pump; stop it so no daemon thread
        # leaks into the rest of the suite (a leaked pump used to hot-spin).
        server.stop()


def test_serve_answers_initialize_over_stdio():
    gate = threading.Event()

    def blocking_wait(*, timeout_seconds):
        # Keep the background pump parked so the test asserts pure framing.
        gate.wait(timeout=5)
        return {"ok": True, "status": "timeout_no_callback"}

    out = _CaptureOut()
    stdin = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    server = CallbackChannelServer(
        stdin=stdin, stdout=out, wait_fn=blocking_wait, ack_fn=lambda *_: {"ok": True},
        route_fn=lambda: {"provider": "claude"},  # pin the Claude seat for pure framing
    )
    try:
        server.serve()  # returns at EOF
        assert len(out.lines) == 1
        assert CHANNEL_CAPABILITY in out.lines[0]
        assert '"id": 1' in out.lines[0] or '"id":1' in out.lines[0]
    finally:
        gate.set()
        server.stop()  # join the background pump so nothing leaks past the test


def test_ping_is_answered_and_unknown_request_is_method_not_found():
    server = CallbackChannelServer(
        wait_fn=lambda *, timeout_seconds: {"ok": True, "status": "timeout_no_callback"},
        ack_fn=lambda *_: {"ok": True},
    )
    assert server.handle_message({"id": 2, "method": "ping"})["result"] == {}
    assert server.handle_message({"method": "notifications/initialized"}) is None
    err = server.handle_message({"id": 3, "method": "tools/call"})
    assert err["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# 5. the push loop paces itself -- an idle/no-manager wait never hot-spins
# ---------------------------------------------------------------------------

def test_pump_loop_backs_off_and_never_hot_spins_when_idle():
    """Regression: a wait_fn that returns WITHOUT blocking (elapsed idle window,
    or a session that is not a verified manager) must make the pump BACK OFF,
    not hot-spin a CPU core. With a large idle backoff the loop makes exactly
    one idle poll and then parks until stopped; a hot-spinning loop would call
    wait_fn thousands of times in the same window -- the bug that pegged a core
    across the whole test suite (and would peg one in any non-manager session).
    """
    import time

    calls = {"n": 0}
    first_poll = threading.Event()

    def wait_fn(*, timeout_seconds):
        calls["n"] += 1
        first_poll.set()
        return {"ok": True, "status": "timeout_no_callback"}  # idle, returns instantly

    server = CallbackChannelServer(
        wait_fn=wait_fn,
        ack_fn=lambda *_: {"ok": True},
        route_fn=lambda: {"provider": "claude"},  # pin the Claude seat: pump must run
        idle_backoff_seconds=30.0,  # large: one idle poll, then park until stop
    )
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})  # starts pump
    try:
        assert first_poll.wait(timeout=5), "pump never ran"
        time.sleep(0.3)  # a hot-spin would rack up thousands of calls in this window
        assert calls["n"] <= 2, f"pump hot-spun: {calls['n']} idle polls in 0.3s"
    finally:
        server.stop()


def test_stop_is_safe_before_pump_ever_starts():
    """``stop()`` must be a no-op-safe idempotent call even if the pump thread
    was never started (no ``initialize`` received)."""
    server = CallbackChannelServer(
        wait_fn=lambda *, timeout_seconds: {"ok": True, "status": "timeout_no_callback"},
        ack_fn=lambda *_: {"ok": True},
    )
    server.stop()
    server.stop()  # idempotent
