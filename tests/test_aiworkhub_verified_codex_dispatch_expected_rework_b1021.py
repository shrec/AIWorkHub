"""Callback dispatch ownership remains extension-process scoped.

A verified Codex manager identity proves coordinator authority, not ownership
of the VS Code callback transport.  Headless MCP children must never start a
second dispatcher merely because they can read a shared route record.
"""
from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core  # noqa: E402


def _readiness(ready: bool = True, repo_id: str = "repo_x", reason: str = ""):
    return types.SimpleNamespace(ready=ready, repo_id=repo_id, reason=reason)


def _patch_core(
    monkeypatch, *,
    readiness,
    bridge_health,
    provider: str = "codex",
    claude_identity=None,
    window_id: str = "",
    codex_identity=None,
    transport: str = "",
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: Path("/tmp/aiworkhub-b1021-repo"))
    monkeypatch.setattr(core.task_store, "storage_readiness", lambda root: readiness)
    fake_bridge = types.SimpleNamespace(
        dispatcher_health=lambda root: dict(bridge_health),
        ensure_dispatcher=lambda *a, **kw: types.SimpleNamespace(
            health=lambda: dict(bridge_health)),
        stop_dispatcher=lambda root: None)
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: fake_bridge)
    monkeypatch.setattr(core, "read_selected_coordinator_target",
                        lambda root=None: {"selected_provider": provider})
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: claude_identity)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: codex_identity)
    monkeypatch.setattr(core, "_canonical_connect",
                        lambda: types.SimpleNamespace(__enter__=lambda: None, __exit__=lambda *a: None, close=lambda: None))
    monkeypatch.setattr(core, "callback_store",
                        types.SimpleNamespace(
                            rebind_pending_callbacks=lambda *a, **kw: 0,
                            seed_missing_review_callbacks=lambda *a, **kw: 0))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", window_id)
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", transport)


_RUNNING = {"dispatcher_running": True, "registered": True, "repo_id": "repo_x", "last_start_error": ""}

_VERIFIED_CODEX = {"provider": "codex", "session_id": str(uuid.uuid4()),
                   "window_id": "codex_vscode_42", "route_state": "available"}
_ROUTE_PENDING_CODEX = {"provider": "codex", "session_id": str(uuid.uuid4()),
                        "window_id": "codex_vscode_43", "route_state": "route_pending"}


def test_verified_codex_health_without_extension_window_not_dispatch_expected(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_RUNNING,
                provider="codex", codex_identity=_VERIFIED_CODEX, window_id="")
    h = core.dispatcher_health()
    assert h["dispatch_expected"] is False


def test_route_pending_health_not_dispatch_expected(monkeypatch):
    """route_pending Codex manager does NOT make dispatch_expected true."""
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_RUNNING,
                provider="codex", codex_identity=_ROUTE_PENDING_CODEX, window_id="")
    h = core.dispatcher_health()
    assert h["dispatch_expected"] is False


def test_headless_health_not_dispatch_expected(monkeypatch):
    """No Codex identity (headless) does NOT make dispatch_expected true."""
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_RUNNING,
                provider="codex", codex_identity=None, window_id="")
    h = core.dispatcher_health()
    assert h["dispatch_expected"] is False


def test_verified_codex_ensure_without_extension_window_stays_headless(monkeypatch):
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_RUNNING,
                provider="codex", codex_identity=_VERIFIED_CODEX, window_id="")
    r = core.dispatcher_ensure_started()
    assert r["status"] == "headless_worker"


def test_route_pending_ensure_stays_headless(monkeypatch):
    """Route_pending ensure_started stays headless_worker."""
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_RUNNING,
                provider="codex", codex_identity=_ROUTE_PENDING_CODEX, window_id="")
    r = core.dispatcher_ensure_started()
    assert r["status"] == "headless_worker"


def test_headless_ensure_stays_headless(monkeypatch):
    """Unidentified ensure_started stays headless_worker."""
    _patch_core(monkeypatch, readiness=_readiness(), bridge_health=_RUNNING,
                provider="codex", codex_identity=None, window_id="")
    r = core.dispatcher_ensure_started()
    assert r["status"] == "headless_worker"


def test_watchdog_recovers_verified_codex(monkeypatch):
    """Watchdog recovers a stopped dispatcher for verified Codex manager."""
    health_calls = {"count": 0}
    health_results = [
        {"dispatch_expected": True, "healthy": False, "status": "stopped",
         "problems": ["dispatcher_stopped"]},
        {"dispatch_expected": True, "healthy": True, "status": "running",
         "problems": []},
    ]

    def fake_health():
        idx = health_calls["count"]
        health_calls["count"] += 1
        return health_results[min(idx, len(health_results) - 1)]

    monkeypatch.setattr(core, "dispatcher_health", fake_health)

    ensure_calls = {"count": 0}

    def fake_ensure():
        ensure_calls["count"] += 1
        return {"ok": True, "status": "started", "dispatcher_started": True,
                "seeded_review_callback_count": 0, "rebound_callback_count": 0}

    monkeypatch.setattr(core, "dispatcher_ensure_started", fake_ensure)
    monkeypatch.setattr(core, "_callback_bridge_module",
                        lambda: types.SimpleNamespace(
                            stop_dispatcher=lambda root: None))

    r = core.dispatcher_watchdog()
    assert r["recovered"] is True
    assert ensure_calls["count"] >= 1
