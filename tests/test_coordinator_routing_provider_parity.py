"""Coordinator routing must resolve from the active verified manager route.

Regression coverage for needfix-NF-2026-00245: a repository with a verified
Claude manager route was permanently stuck reporting ``"automatic: codex"``,
``route_pending``, and reason ``codex_thread_id_not_observed`` -- a Codex-only
reason that a Claude session can never clear -- because
``read_selected_coordinator_target``/``dispatcher_health`` trusted the
persisted (often stale/default) ``coordinator-targets.json`` "selected
provider" instead of the live, verified manager identity for this process.

These tests pin three invariants:
  1. A verified Claude route resolves truthfully to "claude" and never
     surfaces the Codex-only route_pending/codex_thread_id_not_observed pair
     as its own state.
  2. A verified Codex route (and the unverified/no-identity default) is
     byte-for-byte unchanged -- including exact reason strings.
  3. No verified route at all still fails closed to the pre-existing default
     instead of guessing "claude".
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402

_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": str(uuid.uuid4()),
    "window_id": "claude_vscode_3753179",
}


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _write_codex_route(root: Path, *, thread_id: str = "") -> str:
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    repo_id = task_store.storage_readiness(root).repo_id
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": repo_id,
        "selected_provider": "codex",
        "extension_host_pid": 12345,
        "window_id": "window_extension_owned",
        "claim_episode": "episode_pending",
        "targets": {
            "codex": {
                "provider": "codex",
                "capability_state": "route_pending",
                "route": {
                    "repo_id": repo_id,
                    "window_id": "window_extension_owned",
                    "thread_id": thread_id,
                    "session_id": "episode_pending",
                },
            }
        },
    }
    (route_dir / "coordinator-targets.json").write_text(
        json.dumps(route, ensure_ascii=False), encoding="utf-8",
    )
    return repo_id


# ---------------------------------------------------------------------------
# 1. Verified Claude route resolves truthfully.
# ---------------------------------------------------------------------------

def test_claude_verified_route_resolves_to_claude_despite_stale_codex_config(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    _write_codex_route(root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))

    target = core.read_selected_coordinator_target(root)

    assert target["selected_provider"] == "claude"
    claude_target = target["targets"]["claude"]
    assert claude_target["capability_state"] == "available"
    assert claude_target["wake"] == {"mode": "mcp_callback_wait", "supported": True}
    assert "reason" not in claude_target["wake"]
    assert claude_target["route"]["session_id"] == _CLAUDE_IDENTITY["session_id"]


def test_claude_verified_route_resolves_even_when_config_file_absent(tmp_path, monkeypatch):
    root = tmp_path / "repo_no_route_file"
    root.mkdir()
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))

    target = core.read_selected_coordinator_target(root)

    assert target["selected_provider"] == "claude"
    assert target["targets"]["claude"]["capability_state"] == "available"


def test_dispatcher_health_manager_inbox_for_verified_claude_with_stale_codex_config(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    _write_codex_route(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: types.SimpleNamespace(
        dispatcher_health=lambda root: {
            "dispatcher_running": False, "registered": False, "repo_id": "", "last_start_error": "",
        },
    ))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "")
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", "")

    health = core.dispatcher_health()

    assert health["selected_provider"] == "claude"
    assert health["status"] == "manager_inbox"
    assert health["healthy"] is True
    assert health["problems"] == []
    assert "dispatcher_unregistered" not in health["problems"]


# ---------------------------------------------------------------------------
# 2. Verified Codex route is byte-for-byte unchanged.
# ---------------------------------------------------------------------------

def test_codex_route_pending_reason_unchanged_when_no_claude_identity(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    _write_codex_route(root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)

    target = core.read_selected_coordinator_target(root)

    assert target["selected_provider"] == "codex"
    codex_target = target["targets"]["codex"]
    assert codex_target["capability_state"] == "route_pending"
    assert codex_target["wake"] == {
        "mode": "direct_api_or_callback_inbox",
        "supported": False,
        "reason": "codex_thread_id_not_observed",
    }
    assert "claude" not in target["targets"]


def test_codex_route_available_unchanged_when_no_claude_identity(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    _write_codex_route(root, thread_id=thread_id)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)

    target = core.read_selected_coordinator_target(root)

    assert target["selected_provider"] == "codex"
    codex_target = target["targets"]["codex"]
    assert codex_target["capability_state"] == "available"
    assert codex_target["wake"] == {"mode": "app_server_sideband", "supported": True}
    assert "claude" not in target["targets"]


def test_task_create_callback_route_pending_reason_unchanged_for_codex(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_extension_owned",
        "callback_supported": "false",
        "route_state": "route_pending",
    })
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))

    result = core.create_task(
        task_id="TASK_CALLBACK_PENDING_PARITY",
        title="Callback route pending",
        runner="claude_callback_pending",
        topic="task_mcp",
        objective="Codex route_pending reason must stay unchanged.",
        acceptance=["Fails closed."],
        allowed_writes=[],
        read_only=True,
        callback_required=True,
    )

    assert result["ok"] is False
    assert result["stderr"] == "callback_route_pending:codex_thread_id_not_observed"


def test_dispatcher_health_codex_unchanged_when_no_claude_identity(monkeypatch):
    monkeypatch.setattr(core, "repo_root", lambda: Path("/tmp/aiworkhub-parity-repo"))
    monkeypatch.setattr(core.task_store, "storage_readiness",
                        lambda root: types.SimpleNamespace(ready=True, repo_id="repo_x", reason=""))
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: types.SimpleNamespace(
        dispatcher_health=lambda root: {
            "dispatcher_running": False, "registered": False, "repo_id": "repo_x", "last_start_error": "",
        },
    ))
    monkeypatch.setattr(core, "read_selected_coordinator_target",
                        lambda root=None: {"selected_provider": "codex"})
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_extension_owned")
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", "")

    health = core.dispatcher_health()

    assert health["selected_provider"] == "codex"
    assert health["status"] == "stopped"
    assert health["healthy"] is False
    assert "dispatcher_unregistered" in health["problems"]


# ---------------------------------------------------------------------------
# 3. No verified route at all still fails closed -- never defaults to claude.
# ---------------------------------------------------------------------------

def test_no_verified_identity_and_no_config_file_stays_on_preexisting_default(tmp_path, monkeypatch):
    root = tmp_path / "repo_never_initialized"
    root.mkdir()
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)

    target = core.read_selected_coordinator_target(root)

    assert target == {"schema_id": "aiworkhub.coordinator_targets.v1", "selected_provider": "codex"}


def test_no_verified_identity_with_corrupt_config_stays_on_preexisting_default(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    (route_dir / "coordinator-targets.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)

    target = core.read_selected_coordinator_target(root)

    assert target == {"schema_id": "aiworkhub.coordinator_targets.v1", "selected_provider": "codex"}


def test_dispatcher_health_headless_when_no_verified_identity_and_no_window(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: types.SimpleNamespace(
        dispatcher_health=lambda root: {
            "dispatcher_running": False, "registered": False, "repo_id": "", "last_start_error": "",
        },
    ))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "")
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", "")

    health = core.dispatcher_health()

    assert health["selected_provider"] == "codex"
    assert health["dispatch_expected"] is False
