from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import repo_policy, runtime_adapters


def _initialized_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def test_ensure_policy_is_owner_only_idempotent_and_valid(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    path, created = repo_policy.ensure_policy(root)
    assert created is True
    assert path == root / repo_policy.POLICY_RELATIVE_PATH
    policy = repo_policy.load_policy(root)
    assert policy["configured"] is True
    assert policy["tools"]["raw_discovery_forbidden"] == ["grep", "rg", "find", "tree"]
    assert policy["retention"]["worktree_max_bytes"] == 5 * 1024 * 1024 * 1024
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    same, created_again = repo_policy.ensure_policy(root)
    assert same == path
    assert created_again is False


def test_legacy_policy_without_worktree_cap_receives_safe_default(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    value = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    del value["retention"]["worktree_max_bytes"]
    repo_policy.policy_path(root).write_text(json.dumps(value), encoding="utf-8")
    loaded = repo_policy.load_policy(root)
    assert loaded["retention"]["worktree_max_bytes"] == 5 * 1024 * 1024 * 1024


def test_policy_fails_closed_if_mandatory_discovery_denies_are_removed(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    value = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    value["tools"]["raw_discovery_forbidden"] = ["grep"]
    repo_policy.policy_path(root).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(repo_policy.RepoPolicyError, match="mandatory_raw_discovery_denies_missing"):
        repo_policy.load_policy(root)


def test_launch_policy_requires_source_graph_for_initialized_code_task(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    repo_policy.ensure_policy(root)
    card = {"allowed_writes": ["src/change.py"]}
    blocked = repo_policy.validate_launch(root, card, "claude_cli")
    assert blocked == {
        "ok": False,
        "reason": "repo_policy_source_graph_required_for_code",
    }
    card["project_context"] = {
        "task_type": "code",
        "source_graph": {"required": True},
    }
    assert repo_policy.validate_launch(root, card, "claude_cli")["ok"] is True


def test_launch_policy_enforces_provider_scope_and_required_checks(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    value = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    value["providers"]["allowed_adapters"] = ["claude_cli"]
    value["validation"]["required_check_ids"] = ["repo-test"]
    repo_policy.policy_path(root).write_text(json.dumps(value), encoding="utf-8")
    card = {
        "allowed_writes": [],
        "project_context": {"task_type": "research", "source_graph": {"required": False}},
    }
    denied = repo_policy.validate_launch(root, card, "codex_cli")
    assert denied["reason"] == "adapter_denied_by_repo_policy:codex_cli"
    missing = repo_policy.validate_launch(root, card, "claude_cli")
    assert missing["reason"] == "repo_policy_required_checks_missing:repo-test"

    (root / ".aiworkhub/quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {"id": "repo-test", "kind": "test", "command": ["python", "-m", "pytest"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert repo_policy.validate_launch(root, card, "claude_cli")["ok"] is True

    card.update({"callback_required": True, "callback_supported": False})
    callback_blocked = repo_policy.validate_launch(root, card, "claude_cli")
    assert callback_blocked["reason"] == "repo_policy_callback_route_required"


def test_unified_preflight_is_portable_and_truthful_about_unobserved_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _initialized_root(tmp_path)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
                "running": True,
                "registered": True,
                "readable_generation": True,
                "last_success_at": "2026-07-30T14:00:00+00:00",
            "stale_reason": "",
            "build_revision": "aiworkhub.source_graph.semantic.v5",
            "files_seen": 4,
        },
    )
    monkeypatch.setattr(
        repo_policy.worker_workspace, "select_sandbox_backend", lambda: "bubblewrap"
    )
    monkeypatch.setattr(
        repo_policy.task_store,
        "callback_bridge_health",
        lambda _root: {
            "ok": True,
            "backlog_count": 0,
            "retry_count": 0,
            "last_delivered_at": "2026-07-30T14:05:00+00:00",
            "last_dead_letter_at": "",
            "last_dead_letter_error": "",
        },
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "/private/host/bin/model", True, ""
        ),
    )
    monkeypatch.setattr(
        repo_policy.deepseek_credentials,
        "credential_status",
        lambda repo=None: {"launchable": True, "blocker_reason": ""},
    )
    monkeypatch.setattr(
        repo_policy.glm_credentials,
        "credential_status",
        lambda repo=None: {"launchable": True, "blocker_reason": ""},
    )
    monkeypatch.setattr(
        repo_policy.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {
            "launchable": True,
            "access_observed": True,
            "access_state": "granted",
            "blocker_reason": "",
            "window_id": "window_test",
            "live_host_count": 1,
            "stale_host_count": 0,
            "observed_models": ["glm-5.2", "deepseek-v4-pro"],
        },
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": True,
            "authenticated": True,
            "auth_method": "claude.ai",
            "subscription_type": "max",
            "blocker_reason": "",
        },
    )

    report = repo_policy.build_preflight(root)
    assert report["ok"] is True
    by_adapter = {item["adapter_id"]: item for item in report["providers"]}
    assert by_adapter["deepseek_copilot_cli"]["status"] == "ready"
    assert by_adapter["glm_vscode_lm"]["access_observed"] is True
    assert by_adapter["vscode_lm"]["status"] == "ready"
    assert by_adapter["vscode_lm"]["observed_models"] == [
        "glm-5.2",
        "deepseek-v4-pro",
    ]
    assert by_adapter["claude_cli"]["status"] == "ready"
    assert by_adapter["claude_cli"]["access_observed"] is True
    assert by_adapter["claude_cli"]["sandbox_backend"] == "bubblewrap"
    assert by_adapter["vscode_lm"]["sandbox_backend"] == "vscode_lm_in_process"
    assert report["sandbox"]["backend"] == "bubblewrap"
    assert report["sandbox"]["enforceable"] is True
    assert report["sandbox"]["reason"] == ""
    assert report["sandbox"]["native_cli_backend"] == "bubblewrap"
    assert report["sandbox"]["route_aware"] is True
    assert report["source_graph"]["ready_for_code"] is True
    assert report["callback"]["backlog_count"] == 0
    serialized = json.dumps(report, sort_keys=True)
    assert "/private/host" not in serialized
    assert "executable" not in serialized
