"""NF-2026-00288: the callback push transport follows the verified manager seat.

The manager seat is model-agnostic; whoever holds it must get THEIR provider's
push callback activated automatically, decided from the verified manager route
and nothing else. Codex resolves to the existing App Server sideband path
(owned by ``app_server_mux.py`` / ``callback_bridge.py`` -- unchanged here);
Claude resolves to this module's channel; a provider with no push transport
refuses legibly with a named, manager-visible degraded state rather than a
silent downgrade to polling.

These are decision / wire-shape unit tests. Live channel delivery against a real
``claude`` binary is covered by ``test_live_channel_delivery_or_record_obstacle``
below, which records an obstacle (exact version + command) when the binary is
absent -- a unit test is never reported as live delivery.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub.callback_channel import (  # noqa: E402
    CHANNEL_NOTIFICATION_METHOD,
    TRANSPORT_CLAUDE_CHANNEL,
    TRANSPORT_CODEX_APP_SERVER_SIDEBAND,
    CallbackChannelServer,
    build_degraded_notice,
    resolve_push_transport,
)


class _CaptureOut:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. the transport is selected from the verified manager route -- and ONLY it
# ---------------------------------------------------------------------------

def test_claude_seat_activates_the_native_channel_transport():
    decision = resolve_push_transport({"provider": "claude", "session_id": "s"})
    assert decision["transport"] == TRANSPORT_CLAUDE_CHANNEL
    assert decision["active"] is True
    assert decision["owned_here"] is True
    assert decision["push_available"] is True
    assert decision["degraded"] is False
    assert decision["owner"] == "aiworkhub.callback_channel"


def test_transport_follows_the_route_provider_and_nothing_else():
    """Whoever sits in the seat gets THEIR transport: the same resolver returns
    a different transport purely because the route provider differs -- no env,
    cwd, or prose is consulted, and a passed route is used verbatim."""
    claude = resolve_push_transport({"provider": "claude"})
    codex = resolve_push_transport({"provider": "codex"})
    assert claude["transport"] == TRANSPORT_CLAUDE_CHANNEL
    assert codex["transport"] == TRANSPORT_CODEX_APP_SERVER_SIDEBAND
    assert claude["transport"] != codex["transport"]

    # When a route is passed, route_fn (env/ambient) is never consulted.
    calls = {"n": 0}

    def route_fn():
        calls["n"] += 1
        return {"provider": "codex"}

    forced = resolve_push_transport({"provider": "claude"}, route_fn=route_fn)
    assert forced["transport"] == TRANSPORT_CLAUDE_CHANNEL
    assert calls["n"] == 0


def test_resolver_reads_provider_from_route_fn_when_no_route_passed():
    decision = resolve_push_transport(route_fn=lambda: {"provider": "claude"})
    assert decision["transport"] == TRANSPORT_CLAUDE_CHANNEL


# ---------------------------------------------------------------------------
# 2. Codex resolves to the existing sideband path, unchanged
# ---------------------------------------------------------------------------

def test_codex_seat_resolves_to_existing_sideband_path_unchanged():
    decision = resolve_push_transport({"provider": "codex", "thread_id": "t"})
    assert decision["transport"] == TRANSPORT_CODEX_APP_SERVER_SIDEBAND
    assert decision["push_available"] is True
    assert decision["degraded"] is False
    # Owned by the existing Codex path, NOT by this module: the channel neither
    # spawns nor modifies it.
    assert decision["owned_here"] is False
    assert decision["active"] is False
    assert "app_server_mux" in decision["owner"]
    assert "callback_bridge" in decision["owner"]
    assert "callback_channel" not in decision["owner"]


def test_codex_seat_keeps_the_channel_dormant():
    """A Codex seat must NOT start this channel's push pump: its push path is
    the sideband mux, owned elsewhere. The channel answers the handshake and
    stays dormant -- no push pump, no channel event, no degraded notice."""
    out = _CaptureOut()
    wait_calls = {"n": 0}

    def wait_fn(*, timeout_seconds):
        wait_calls["n"] += 1
        return {"ok": True, "status": "timeout_no_callback"}

    server = CallbackChannelServer(
        stdout=out,
        wait_fn=wait_fn,
        ack_fn=lambda *_: {"ok": True},
        route_fn=lambda: {"provider": "codex"},
    )
    try:
        server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        time.sleep(0.2)  # give any erroneously-started pump a chance to run
        assert wait_calls["n"] == 0  # dormant: no push pump for a Codex seat
        assert out.lines == []  # nothing pushed to the wire
        assert server._activation["transport"] == TRANSPORT_CODEX_APP_SERVER_SIDEBAND
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 3. a provider with no push transport refuses legibly (named degraded state)
# ---------------------------------------------------------------------------

def test_provider_without_transport_degrades_legibly_not_silently():
    decision = resolve_push_transport({"provider": "gemini"})
    assert decision["degraded"] is True
    assert decision["push_available"] is False
    assert decision["pull_inbox_active"] is True
    assert decision["manager_visible"] is True
    assert decision["transport"] is None
    reason = decision["reason"].lower()
    assert "gemini" in reason           # names WHICH provider
    assert "push" in reason             # says push is unavailable
    assert "pull inbox" in reason       # says the pull inbox is in use


def test_unverified_seat_is_a_named_degraded_state_too():
    decision = resolve_push_transport({})
    assert decision["provider"] == "unverified"
    assert decision["degraded"] is True
    assert decision["pull_inbox_active"] is True
    assert decision["transport"] is None


def test_degraded_provider_surfaces_manager_visible_notice_and_never_polls():
    """When this channel is spawned for a seat whose provider has no push
    transport, it must emit exactly ONE manager-visible degraded notice and
    NOT start silently polling."""
    out = _CaptureOut()
    wait_calls = {"n": 0}

    def wait_fn(*, timeout_seconds):
        wait_calls["n"] += 1
        return {"ok": True, "status": "timeout_no_callback"}

    server = CallbackChannelServer(
        stdout=out,
        wait_fn=wait_fn,
        ack_fn=lambda *_: {"ok": True},
        route_fn=lambda: {"provider": "gemini"},
    )
    try:
        server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        time.sleep(0.2)
        assert wait_calls["n"] == 0       # never silently polls
        assert len(out.lines) == 1        # exactly one degraded notice
        notice = out.lines[0]
        assert CHANNEL_NOTIFICATION_METHOD in notice
        assert "gemini" in notice
        assert "DEGRADED" in notice
    finally:
        server.stop()


def test_build_degraded_notice_is_field_safe():
    notice = build_degraded_notice({"provider": "gemini"})
    assert notice["method"] == CHANNEL_NOTIFICATION_METHOD
    assert "id" not in notice  # a notification never carries an id
    meta = notice["params"]["meta"]
    assert meta["provider"] == "gemini"
    assert meta["push_available"] == "false"
    assert meta["pull_inbox_active"] == "true"
    for key in meta:  # meta keys are bounded identifiers per the channel contract
        assert re.fullmatch(r"[A-Za-z0-9_]+", key), key


# ---------------------------------------------------------------------------
# 4. the Claude seat starts THIS channel's native push pump
# ---------------------------------------------------------------------------

def test_claude_seat_starts_the_channel_push_pump():
    pumped = threading.Event()
    session = "sess-1"
    state = {"n": 0}

    def wait_fn(*, timeout_seconds):
        state["n"] += 1
        pumped.set()
        if state["n"] == 1:
            return {
                "ok": True, "status": "callback_ready", "provider": "claude",
                "batch_id": "b1", "lease_id": "l1", "origin_thread_id": session,
                "members": [{"task_id": "T1", "state": "review_ready"}],
            }
        return {"ok": True, "status": "timeout_no_callback"}

    out = _CaptureOut()
    server = CallbackChannelServer(
        stdout=out,
        wait_fn=wait_fn,
        ack_fn=lambda *_: {"ok": True},
        route_fn=lambda: {"provider": "claude", "session_id": session},
        idle_backoff_seconds=30.0,
    )
    try:
        server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert pumped.wait(timeout=5), "claude seat never started the push pump"
        assert server._activation["transport"] == TRANSPORT_CLAUDE_CHANNEL
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 5. live delivery -- a real event into a real session, or a recorded obstacle
# ---------------------------------------------------------------------------

def test_live_channel_delivery_or_record_obstacle():
    """Confirm push parity against a REAL Claude Code binary, or record the
    exact obstacle. This is NOT a wire-shape unit test: it either exercises a
    real ``claude`` channels session or skips with the exact version + command,
    so a green unit suite is never mistaken for verified live delivery."""
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip(
            "OBSTACLE: no 'claude' binary on PATH in this sandbox; live channel "
            "delivery unverifiable. Verify manually after registering the server "
            "in .mcp.json with: `claude --dangerously-load-development-channels "
            "server:aiworkhub-callback-channel`."
        )
    version = subprocess.run(
        [claude, "--version"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    help_text = subprocess.run(
        [claude, "--help"], capture_output=True, text=True, timeout=30
    ).stdout
    if "channel" not in help_text.lower():
        pytest.skip(
            f"OBSTACLE: installed claude {version!r} exposes no channels flag "
            "(`--dangerously-load-development-channels` absent from --help); "
            "live delivery unverifiable on this version."
        )
    pytest.skip(
        f"OBSTACLE: claude {version!r} advertises channels, but end-to-end push "
        "needs a live interactive session and cannot be asserted from a headless "
        "unit run. Confirm with `claude --dangerously-load-development-channels "
        "server:aiworkhub-callback-channel` plus a real review callback."
    )
