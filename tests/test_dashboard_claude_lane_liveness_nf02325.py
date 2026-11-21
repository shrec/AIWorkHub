"""NF-2026-02325: the dashboard derives a live Claude Code manager cross-process.

Fakes only: a temporary sessions directory, a fake liveness check and a fake
repository root. No real Claude CLI, manager loop or manager seat is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard, dashboard_mcp_app, manager_route_probe  # noqa: E402

SESSION = "0b6d1c62-3f4e-4a8b-9c1d-2e3f4a5b6c7d"
OTHER_SESSION = "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
PID = 4242
CODEX_REASON = "codex_route_not_live:claim_with_aiworkhub_manager_callback_lane_claim"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    monkeypatch.setattr(core, "repo_root", lambda: root)
    return root


@pytest.fixture
def sessions(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "home" / ".claude" / "sessions"
    directory.mkdir(parents=True)
    monkeypatch.setattr(manager_route_probe, "claude_sessions_dir", lambda: directory)
    return directory


@pytest.fixture
def alive(monkeypatch) -> set[int]:
    live_pids: set[int] = set()
    monkeypatch.setattr(manager_route_probe, "pid_alive", lambda pid: pid in live_pids)
    return live_pids


def _descriptor(repo: Path, **overrides: Any) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "pid": PID,
        "sessionId": SESSION,
        "cwd": str(repo),
        "kind": "interactive",
        "entrypoint": "claude-vscode",
    }
    descriptor.update(overrides)
    return descriptor


def _write(directory: Path, pid: int, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (directory / f"{pid}.json").write_text(text, encoding="utf-8")


# --- manager_route_probe.claude_session_live --------------------------------


def test_live_descriptor_is_live(repo, sessions, alive):
    _write(sessions, PID, _descriptor(repo))
    alive.add(PID)

    assert manager_route_probe.claude_session_live(SESSION) == {
        "live": True,
        "reason": "verified",
        "window_id": f"claude_vscode_{PID}",
    }


def test_injected_dir_and_liveness_are_honoured(repo, tmp_path):
    directory = tmp_path / "injected"
    directory.mkdir()
    _write(directory, PID, _descriptor(repo))

    live = manager_route_probe.claude_session_live(
        SESSION, sessions_dir=directory, is_alive=lambda pid: pid == PID
    )
    dead = manager_route_probe.claude_session_live(
        SESSION, sessions_dir=directory, is_alive=lambda _pid: False
    )

    assert live["live"] is True
    assert dead == {"live": False, "reason": "pid_not_alive", "window_id": ""}


def test_dead_pid_is_not_live(repo, sessions, alive):
    _write(sessions, PID, _descriptor(repo))

    result = manager_route_probe.claude_session_live(SESSION)

    assert result == {"live": False, "reason": "pid_not_alive", "window_id": ""}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"cwd": "/somewhere/else"}, "descriptor_cwd_mismatch"),
        ({"entrypoint": "cli"}, "descriptor_entrypoint"),
        ({"kind": "background"}, "descriptor_kind"),
        ({"pid": PID + 1}, "descriptor_pid_mismatch"),
    ],
    ids=["foreign_cwd", "wrong_entrypoint", "wrong_kind", "pid_mismatch"],
)
def test_foreign_or_invalid_descriptor_is_not_live(repo, sessions, alive, overrides, reason):
    _write(sessions, PID, _descriptor(repo, **overrides))
    alive.add(PID)

    result = manager_route_probe.claude_session_live(SESSION)

    assert result["live"] is False
    assert result["reason"] == reason


@pytest.mark.parametrize("payload", ["{not json", "[1, 2, 3]", "", "\"text\""])
def test_malformed_descriptor_is_skipped(repo, sessions, alive, payload):
    _write(sessions, PID, payload)
    alive.add(PID)

    assert manager_route_probe.claude_session_live(SESSION) == {
        "live": False,
        "reason": "descriptor_not_found",
        "window_id": "",
    }


def test_other_sessions_and_non_pid_files_are_ignored(repo, sessions, alive):
    _write(sessions, PID, _descriptor(repo, sessionId=OTHER_SESSION))
    (sessions / "settings.json").write_text(json.dumps(_descriptor(repo)), encoding="utf-8")
    alive.add(PID)

    assert manager_route_probe.claude_session_live(SESSION)["live"] is False
    assert manager_route_probe.claude_session_live(OTHER_SESSION)["live"] is True


def test_oversized_descriptor_is_skipped(repo, sessions, alive, monkeypatch):
    monkeypatch.setattr(manager_route_probe, "CLAUDE_DESCRIPTOR_MAX_BYTES", 16)
    _write(sessions, PID, _descriptor(repo))
    alive.add(PID)

    assert manager_route_probe.claude_session_live(SESSION)["reason"] == "descriptor_not_found"


def test_scan_is_bounded(repo, sessions, alive, monkeypatch):
    monkeypatch.setattr(manager_route_probe, "CLAUDE_SESSION_SCAN_LIMIT", 3)
    for pid in range(1, 6):
        _write(sessions, pid, _descriptor(repo, pid=pid, sessionId=OTHER_SESSION))

    assert len(manager_route_probe._claude_descriptor_candidates(sessions)) == 3


def test_missing_session_and_missing_dir_never_raise(repo, tmp_path):
    assert manager_route_probe.claude_session_live("")["reason"] == "session_id_missing"
    missing = manager_route_probe.claude_session_live(SESSION, sessions_dir=tmp_path / "absent")
    assert missing == {"live": False, "reason": "sessions_dir_unreadable", "window_id": ""}


def test_a_raising_liveness_check_never_escapes(repo, sessions):
    _write(sessions, PID, _descriptor(repo))

    def boom(_pid: int) -> bool:
        raise RuntimeError("liveness exploded")

    result = manager_route_probe.claude_session_live(SESSION, is_alive=boom)

    assert result == {"live": False, "reason": "probe_failed:RuntimeError", "window_id": ""}


def test_posix_liveness_semantics(monkeypatch):
    monkeypatch.setattr(manager_route_probe, "_windows", lambda: False)

    def kill_with(error: BaseException | None):
        def fake_kill(_pid: int, _signal: int) -> None:
            if error is not None:
                raise error

        return fake_kill

    monkeypatch.setattr(manager_route_probe.os, "kill", kill_with(None))
    assert manager_route_probe.pid_alive(PID) is True
    monkeypatch.setattr(manager_route_probe.os, "kill", kill_with(PermissionError()))
    assert manager_route_probe.pid_alive(PID) is True
    monkeypatch.setattr(manager_route_probe.os, "kill", kill_with(ProcessLookupError()))
    assert manager_route_probe.pid_alive(PID) is False


@pytest.mark.parametrize("pid", [0, -1, True, "42"])
def test_invalid_pid_is_not_alive(pid):
    assert manager_route_probe.pid_alive(pid) is False


def test_own_process_is_alive_on_this_host():
    import os

    assert manager_route_probe.pid_alive(os.getpid()) is True


# --- dashboard_mcp_app.manager_identity_target -------------------------------


def _claude_targets(capability_state: str = "callback_required") -> dict[str, Any]:
    return {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "selected_provider": "claude",
        "targets": {
            "claude": {
                "route": {"thread_id": "", "session_id": SESSION, "window_id": ""},
                "capability_state": capability_state,
                "wake": {"mode": "mcp_callback_wait", "supported": False, "reason": "callback_required"},
            }
        },
    }


def _codex_pending_targets() -> dict[str, Any]:
    return {
        "selected_provider": "codex",
        "targets": {
            "codex": {
                "route": {"thread_id": "", "session_id": ""},
                "capability_state": "route_pending",
                "wake": {"mode": "direct_api_or_callback_inbox", "supported": False, "reason": "codex_thread_id_not_observed"},
            }
        },
    }


def _manager(**extra: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {"ok": True, "role": "dispatcher", "provider": "codex", "manager_route": {}}
    reply.update(extra)
    return reply


def _snapshot_target(monkeypatch, repo: Path, targets: dict[str, Any], manager: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setattr(dashboard, "build_snapshot", lambda **_kwargs: {"tasks": {}, "status_counts": {}})
    monkeypatch.setattr(core, "manager_bootstrap", lambda: dict(manager))
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True, "status": "running"})
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: targets)
    monkeypatch.setattr(dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", lambda _root: {})
    monkeypatch.setattr(dashboard_mcp_app.cli_model_discovery, "codex_models", lambda **_kwargs: [])
    monkeypatch.setattr(dashboard_mcp_app.cli_model_discovery, "claude_models", lambda _root: [])
    return dashboard_mcp_app.snapshot_view(full=True)["manager_identity_target"]


def test_live_claude_session_publishes_available_callback_lane(monkeypatch, repo, sessions, alive):
    _write(sessions, PID, _descriptor(repo))
    alive.add(PID)

    target = _snapshot_target(monkeypatch, repo, _claude_targets(), _manager())

    assert target == {
        "selected_provider": "claude",
        "capability_state": "available",
        "reason": "",
        "callback_lane": "claude_callback_wait",
        "session_id": SESSION,
    }


def test_dead_claude_session_keeps_persisted_state_with_reason(monkeypatch, repo, sessions, alive):
    _write(sessions, PID, _descriptor(repo))

    target = _snapshot_target(monkeypatch, repo, _claude_targets(), _manager())

    assert target == {
        "selected_provider": "claude",
        "capability_state": "callback_required",
        "reason": "claude_session_not_running",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"cwd": "/foreign/repo"},
        {"entrypoint": "cli"},
        "{malformed",
    ],
    ids=["foreign_cwd", "wrong_entrypoint", "malformed_json"],
)
def test_foreign_or_malformed_descriptor_is_not_running(monkeypatch, repo, sessions, alive, payload):
    _write(sessions, PID, payload if isinstance(payload, str) else _descriptor(repo, **payload))
    alive.add(PID)

    target = _snapshot_target(monkeypatch, repo, _claude_targets(), _manager())

    assert target["capability_state"] == "callback_required"
    assert target["reason"] == "claude_session_not_running"
    assert "callback_lane" not in target


def test_claude_target_without_a_claimed_session_is_not_running(monkeypatch, repo, sessions, alive):
    targets = _claude_targets()
    targets["targets"]["claude"]["route"]["session_id"] = ""

    target = _snapshot_target(monkeypatch, repo, targets, _manager())

    assert target["reason"] == "claude_session_not_running"
    assert "callback_lane" not in target


@pytest.mark.parametrize("where", ["manager_route", "manager_chat_seat"])
def test_manager_chat_seat_still_wins(monkeypatch, repo, sessions, alive, where):
    _write(sessions, PID, _descriptor(repo))
    alive.add(PID)
    seat = {"session_id": "mls-aaaa1111bbbb2222", "backend_id": "claude_cli", "model": "claude-opus-5-5"}
    manager = _manager(manager_route={"manager_chat": seat}) if where == "manager_route" else _manager(manager_chat_seat=seat)

    target = _snapshot_target(monkeypatch, repo, _claude_targets(), manager)

    assert target == {
        "selected_provider": "manager_chat",
        "capability_state": "ready",
        "reason": "",
        **seat,
    }


def test_codex_route_pending_names_the_claim_action(monkeypatch, repo, sessions, alive):
    target = _snapshot_target(monkeypatch, repo, _codex_pending_targets(), _manager())

    assert target == {
        "selected_provider": "codex",
        "capability_state": "route_pending",
        "reason": CODEX_REASON,
    }


def test_codex_available_route_is_unchanged():
    target = dashboard_mcp_app._provider_identity_target(
        "codex",
        {"capability_state": "available", "wake": {"mode": "app_server_sideband", "supported": True}},
    )

    assert target == {"selected_provider": "codex", "capability_state": "available", "reason": None}


def test_no_selected_provider_is_unchanged():
    assert dashboard_mcp_app._provider_identity_target("", {}) == {
        "selected_provider": "",
        "capability_state": None,
        "reason": None,
    }


# --- review corrections: no symlink / non-regular reads, no stale "available" --------


def test_directory_named_like_a_descriptor_is_skipped(repo, sessions, alive):
    (sessions / f"{PID}.json").mkdir()
    alive.add(PID)

    assert manager_route_probe._claude_descriptor_candidates(sessions) == []
    assert manager_route_probe.claude_session_live(SESSION)["reason"] == "descriptor_not_found"


def test_symlinked_descriptor_is_not_followed(repo, sessions, alive, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_descriptor(repo)), encoding="utf-8")
    try:
        (sessions / f"{PID}.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this host")
    alive.add(PID)

    assert manager_route_probe._claude_descriptor_candidates(sessions) == []
    assert manager_route_probe.claude_session_live(SESSION)["reason"] == "descriptor_not_found"


def test_dead_session_downgrades_a_stale_available_state(monkeypatch, repo, sessions, alive):
    _write(sessions, PID, _descriptor(repo))

    target = _snapshot_target(monkeypatch, repo, _claude_targets("available"), _manager())

    assert target == {
        "selected_provider": "claude",
        "capability_state": "callback_required",
        "reason": "claude_session_not_running",
    }
