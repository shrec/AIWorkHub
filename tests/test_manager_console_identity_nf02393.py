"""NF-2026-02393: the Manager Chat panel works in a Claude-selected window.

The panel's MCP child of the VS Code extension host gets a verified repo-local
manager-console identity bound to the window route, independent of
``selected_provider``, without claiming the Claude callback lane or changing
the dispatcher decision. All process ancestry is monkeypatched; no provider
CLI, manager loop session or manager seat is started or claimed.

Run: python -m pytest -q tests/test_manager_console_identity_nf02393.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import core, manager_console_identity, manager_route_probe, task_store  # noqa: E402

_WINDOW = "window_57822dcf3bb20aee129495ba"
_EPISODE = "episode_claude_selected"
_HOST_PID = 62064


def _write_route(root: Path, **overrides) -> dict:
    repo_id = task_store.storage_readiness(root).repo_id
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": repo_id,
        "selected_provider": "claude",
        "extension_host_pid": _HOST_PID,
        "window_id": _WINDOW,
        "claim_episode": _EPISODE,
        "targets": {
            "codex": {"provider": "codex", "capability_state": "route_pending",
                      "route": {"repo_id": repo_id, "window_id": _WINDOW, "thread_id": "", "session_id": _EPISODE}},
            "claude": {"provider": "claude", "capability_state": "callback_required"},
        },
    }
    route.update(overrides)
    path = root / ".aiworkhub" / "config" / "routing" / "coordinator-targets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(route, ensure_ascii=False), encoding="utf-8")
    return route


@pytest.fixture
def claude_window(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    _write_route(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", _WINDOW)
    monkeypatch.delenv("AIWORKHUB_MANAGER_SEAT_BACKEND", raising=False)
    monkeypatch.delenv("AIWORKHUB_MANAGER_SEAT_TOKEN", raising=False)
    monkeypatch.setattr(core, "_pid_in_same_uid_ancestor_chain", lambda pid, *, max_depth: pid == _HOST_PID)
    monkeypatch.setattr(manager_route_probe, "claude_ancestry_present", lambda: False)
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: None)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    monkeypatch.setattr(core, "_schedule_task_hygiene", lambda **kw: {"status": "not_evaluated"})
    monkeypatch.setattr(core, "_stamp_seat_scratch_once", lambda *a, **kw: None)
    monkeypatch.setattr(
        manager_route_probe,
        "probe_claude_route",
        lambda: SimpleNamespace(report=lambda: {"reason": "hop_limit_exceeded", "observations": {}}),
    )
    return root


_EXPECTED = {
    "provider": "manager_console",
    "session_id": _EPISODE,
    "thread_id": "",
    "window_id": _WINDOW,
    "callback_supported": "false",
    "route_state": "repo_local",
}


def test_claude_selected_window_child_gets_the_console_identity(claude_window):
    assert manager_console_identity.verified_identity() == _EXPECTED
    assert core._live_manager_identity() == _EXPECTED


def test_session_falls_back_to_window_id_without_claim_episode(claude_window):
    _write_route(claude_window, claim_episode="")
    assert manager_console_identity.verified_identity()["session_id"] == _WINDOW


def test_bootstrap_reports_a_verified_manager_that_is_not_claude(claude_window):
    reply = core.manager_bootstrap()

    assert reply["role"] == "manager"
    assert reply["manager_verified"] is True
    assert reply["provider"] == "manager_console"
    assert reply["manager_route"]["session_id"] == _EPISODE
    assert reply["manager_route"]["window_id"] == _WINDOW
    assert reply["manager_route"]["callback_supported"] == "false"
    assert "manager_chat_seat_observed" not in reply


def test_bootstrap_call_starts_no_dispatcher_and_adopts_no_callbacks(claude_window, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("the console identity must not touch the callback lane")

    monkeypatch.setattr(core, "dispatcher_ensure_started", forbidden)
    monkeypatch.setattr(core, "_canonical_connect", forbidden)
    monkeypatch.setattr(core, "_bootstrap_task_health", lambda *a, **kw: {"status": "not_evaluated"})

    reply = core.manager_bootstrap(bootstrap_call=True)

    assert reply["dispatcher"]["status"] == "not_applicable"
    assert reply["callback_adoption"]["adopted"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda mp, root: mp.delenv("AIWORKHUB_WINDOW_ID"), id="window_env_unset"),
        pytest.param(lambda mp, root: mp.setenv("AIWORKHUB_WINDOW_ID", "window_other"), id="window_differs"),
        pytest.param(lambda mp, root: _write_route(root, repo_id="repo_" + "0" * 32), id="repo_id_differs"),
        pytest.param(lambda mp, root: _write_route(root, extension_host_pid=1), id="host_pid_le_1"),
        pytest.param(lambda mp, root: _write_route(root, extension_host_pid=0), id="host_pid_missing"),
        pytest.param(lambda mp, root: _write_route(root, extension_host_pid=4242), id="host_pid_not_ancestor"),
        pytest.param(lambda mp, root: _write_route(root, extension_host_pid="not-a-pid"), id="host_pid_garbage"),
        pytest.param(lambda mp, root: mp.setattr(manager_route_probe, "claude_ancestry_present", lambda: True),
                     id="claude_ancestry_present"),
        pytest.param(lambda mp, root: (root / ".aiworkhub" / "config" / "routing" / "coordinator-targets.json").unlink(),
                     id="targets_missing"),
        pytest.param(lambda mp, root: (root / ".aiworkhub" / "config" / "routing" / "coordinator-targets.json")
                     .write_text("{not json", encoding="utf-8"), id="targets_unreadable"),
        pytest.param(lambda mp, root: _write_route(root, selected_provider="nobody"), id="no_provider_selected"),
    ],
)
def test_each_binding_check_is_load_bearing(claude_window, monkeypatch, mutate):
    mutate(monkeypatch, claude_window)

    assert manager_console_identity.verified_identity() is None
    assert core._live_manager_identity() is None
    assert core.manager_bootstrap()["role"] == "worker_or_unverified_client"


def test_targets_read_error_fails_closed(claude_window, monkeypatch):
    def unreadable(root=None):
        raise OSError("denied")

    monkeypatch.setattr(core, "read_selected_coordinator_target", unreadable)
    assert manager_console_identity.verified_identity() is None


def test_the_claude_callback_lane_still_refuses_the_console(claude_window):
    # Real lane refusal (no patch): the console identity must not open or own the Claude lane.
    assert core._live_manager_identity() == _EXPECTED

    assert core.claude_callback_wait(1) == {"ok": False, "reason": "verified_claude_manager_required"}
    assert core.claude_callback_ack("batch", "lease") == {"ok": False, "reason": "verified_claude_manager_required"}


def test_dispatcher_health_decision_is_unchanged_by_the_console(claude_window, monkeypatch):
    monkeypatch.setattr(
        core, "_callback_bridge_module",
        lambda: SimpleNamespace(dispatcher_health=lambda root: {"dispatcher_running": False, "registered": False}),
    )
    monkeypatch.setattr(core, "_passive_codex_manager_context", lambda root, target: {})
    monkeypatch.setattr(core.task_store, "callback_bridge_health",
                        lambda root: {"backlog_count": 0, "oldest_pending_at": ""}, raising=False)
    keys = ("status", "dispatch_expected", "problems", "selected_provider", "healthy")

    with_console = core.dispatcher_health()
    monkeypatch.setattr(manager_console_identity, "verified_identity", lambda: None)
    without_console = core.dispatcher_health()

    assert {k: with_console[k] for k in keys} == {k: without_console[k] for k in keys}


def test_codex_selected_window_keeps_the_codex_identity_first(claude_window, monkeypatch):
    _write_route(claude_window, selected_provider="codex")
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: core._codex_extension_route_manager_identity())

    identity = core._live_manager_identity()

    assert identity["provider"] == "codex"
    assert identity["route_state"] == "route_pending"
    assert core._codex_extension_route_manager_identity() is not None


def test_codex_extension_route_still_requires_codex_selection(claude_window):
    assert core._codex_extension_route_manager_identity() is None


def test_chat_record_alone_is_still_not_an_identity(claude_window, monkeypatch):
    chat = {"session_id": "mls-" + "ab" * 16, "backend_id": "opencode_cli", "model": "m", "status": "active"}
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: dict(chat))
    monkeypatch.delenv("AIWORKHUB_WINDOW_ID")

    reply = core.manager_bootstrap()

    assert reply["role"] == "worker_or_unverified_client"
    assert reply["manager_chat_seat_observed"]["session_id"] == chat["session_id"]


def test_manager_loop_status_forwards_with_the_verified_repo_root(claude_window, monkeypatch):
    from aiworkhub import server

    captured: dict = {}

    def fake_status(repo):
        captured["repo"] = repo
        return {"ok": True, "fake": True}

    monkeypatch.setattr(server.manager_loop_service, "status", fake_status)

    assert server.aiworkhub_manager_loop_status() == {"ok": True, "fake": True}
    assert captured["repo"] == claude_window.resolve()


def test_manager_loop_authority_resolves_the_same_repo(claude_window):
    from aiworkhub import manager_loop

    assert manager_loop._manager_authority(claude_window) == claude_window.resolve()


def test_manager_loop_authority_still_refuses_without_identity(claude_window, monkeypatch):
    from aiworkhub import manager_loop

    monkeypatch.delenv("AIWORKHUB_WINDOW_ID")
    with pytest.raises(manager_loop.ManagerLoopError, match="verified_manager_identity_required"):
        manager_loop._manager_authority(claude_window)
