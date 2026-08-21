from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    repo_policy,
    runtime_adapters,
    vscode_lm_bridge,
    workforce_catalog,
    workforce_router,
    worker_workspace,
)


def _initialized_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _ready_preflight_deps(monkeypatch: pytest.MonkeyPatch) -> None:
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
            "last_success_at": "2026-08-03T00:00:00+00:00",
            "build_revision": "aiworkhub.source_graph.semantic.v5",
            "files_seen": 1,
        },
    )
    monkeypatch.setattr(repo_policy.task_store, "callback_bridge_health", lambda _root: {"ok": True})
    monkeypatch.setattr(repo_policy.workspace_hygiene, "inventory", lambda _root, refresh_sizes=False: {})
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(adapter_id, "/bin/model", True, ""),
    )


def test_windows_sandbox_selection_fails_closed_without_appcontainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)
    with pytest.raises(worker_workspace.WorkspaceError, match="windows_appcontainer_sandbox_unavailable"):
        worker_workspace.select_sandbox_backend()


@pytest.mark.parametrize(
    "adapter_id",
    ["vscode_lm", "glm_vscode_lm", "deepseek_vscode_lm"],
)
def test_editor_model_launch_selects_in_process_boundary_before_host_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    adapter_id: str,
) -> None:
    monkeypatch.setattr(
        process_launcher,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError("windows_appcontainer_sandbox_unavailable")
        ),
    )

    assert (
        process_launcher._sandbox_backend_for_adapter(adapter_id)
        == worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND
    )


def test_editor_response_applier_is_only_unsandboxed_for_editor_adapters(tmp_path: Path) -> None:
    workspace = SimpleNamespace(
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        repo=tmp_path / "repo",
        allowed_writes=("out/result.json",),
    )
    argv = [sys.executable, "-m", "aiworkhub.vscode_lm_worker"]

    assert worker_workspace.sandbox_argv(
        workspace,
        "glm_vscode_lm",
        argv,
        backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
    ) == argv
    with pytest.raises(worker_workspace.WorkspaceError, match="adapter_forbidden"):
        worker_workspace.sandbox_argv(
            workspace,
            "claude_cli",
            argv,
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
        )


def test_finalizer_reuses_exact_editor_route_instead_of_windows_appcontainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_launcher,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError(
                "windows_appcontainer_sandbox_unavailable"
            )
        ),
    )

    route = process_launcher._validation_route_kwargs({
        "adapter_id": "vscode_lm",
        "sandbox_backend": "vscode_lm_in_process",
    })

    assert route == {
        "backend": "vscode_lm_in_process",
        "adapter_id": "vscode_lm",
    }


def test_finalizer_rejects_recorded_backend_drift() -> None:
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_route_backend_mismatch",
    ):
        process_launcher._validation_route_kwargs({
            "adapter_id": "vscode_lm",
            "sandbox_backend": "landlock",
        })


def test_validation_only_replay_accepts_deterministic_lane_without_provider_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_launcher,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError(
                "windows_appcontainer_sandbox_unavailable"
            )
        ),
    )

    route = process_launcher._validation_route_kwargs({
        "adapter_id": "vscode_lm",
        "sandbox_backend": "deterministic_validation",
        "execution_mode": "validation_only_replay",
        "provider_launched": False,
    })

    assert route == {
        "backend": "vscode_lm_in_process",
        "adapter_id": "vscode_lm",
    }


def test_deterministic_lane_is_forbidden_outside_validation_only_replay() -> None:
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_route_backend_mismatch",
    ):
        process_launcher._validation_route_kwargs({
            "adapter_id": "vscode_lm",
            "sandbox_backend": "deterministic_validation",
            "execution_mode": "provider_worker",
        })


def test_windows_native_cli_plan_requires_appcontainer_grade_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"MZ")
    executable.chmod(0o755)
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: True)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _binary: str(executable))

    plan = runtime_adapters.build_runtime_command("claude_cli", "Prompt", repo)

    assert plan.launchable is False
    assert plan.validation_reason == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER


def test_windows_preflight_excludes_native_cli_but_keeps_editor_bridge_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _initialized_root(tmp_path)
    _ready_preflight_deps(monkeypatch)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        repo_policy.worker_workspace,
        "finalization_preflight_probe_nonblocking",
        lambda _root, _adapter: {
            "ok": True,
            "status": "ready",
            "reason": "",
            "phase": "preflight_finalization",
        },
    )
    monkeypatch.setattr(
        repo_policy.worker_workspace,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(worker_workspace.WorkspaceError("windows_appcontainer_sandbox_unavailable")),
    )
    monkeypatch.setattr(
        repo_policy.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {
            "launchable": True,
            "blocker_reason": "",
            "window_id": "window_test",
            "live_host_count": 1,
            "stale_host_count": 0,
            "observed_models": ["glm-5.2", "deepseek-chat"],
        },
    )

    report = repo_policy.build_preflight(root)
    by_adapter = {item["adapter_id"]: item for item in report["providers"]}

    assert by_adapter["claude_cli"]["launchable"] is False
    assert by_adapter["claude_cli"]["status"] == "sandbox_unavailable"
    assert by_adapter["claude_cli"]["reason"] == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    assert by_adapter["claude_cli"]["sandbox_backend"] == ""
    assert by_adapter["glm_vscode_lm"]["launchable"] is True
    assert by_adapter["glm_vscode_lm"]["sandbox_backend"] == "vscode_lm_in_process"
    assert report["status"] == "ready"
    assert report["provider_summary"]["coverage_status"] == "full"
    assert report["provider_summary"]["unavailable_route_count"] == 0
    assert report["provider_summary"]["excluded_route_count"] == 4


def test_windows_workforce_allocation_uses_only_launchable_editor_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _initialized_root(tmp_path)
    _ready_preflight_deps(monkeypatch)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        repo_policy.worker_workspace,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError(
                "windows_appcontainer_sandbox_unavailable"
            )
        ),
    )
    monkeypatch.setattr(
        repo_policy.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {
            "launchable": True,
            "blocker_reason": "",
            "window_id": "window_test",
            "live_host_count": 1,
            "stale_host_count": 0,
            "observed_models": ["deepseek-v4-pro", "glm-5.2"],
        },
    )

    preflight = repo_policy.build_preflight(root)
    catalog = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight=preflight,
    )
    native_adapters = {
        "claude_cli",
        "codex_cli",
        "deepseek_copilot_cli",
        "glm_copilot_cli",
    }
    assert all(
        worker["available"] is False
        for worker in catalog["workers"]
        if worker["effective_adapter_id"] in native_adapters
    )

    task = workforce_router.TaskRequirements.build(
        task_id="T-windows-editor-route",
        repo_id="repo_test",
        kinds=["code"],
        risk="high",
        owner_model_pin="glm-5.2",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=catalog)

    assert decision["selected_worker_id"] == "glm-5.2"
    assert decision["selected_adapter_id"] == "glm_vscode_lm"
    assert decision["launch_contract"] == {
        "runner": "glm_5.2",
        "adapter_id": "glm_vscode_lm",
        "model": "glm-5.2",
        "task_id": "T-windows-editor-route",
        "identity_rule": "use_same_runner_for_task_create_and_agent_launch_task",
    }


def test_editor_model_aliases_resolve_only_to_observed_same_provider_models() -> None:
    observed = ["deepseek-chat", "z-ai/glm-5.2", "anthropic/claude-sonnet-4"]

    assert (
        vscode_lm_bridge.resolve_editor_model_alias("deepseek-v4-pro", observed)
        == "deepseek-chat"
    )
    assert vscode_lm_bridge.resolve_editor_model_alias("glm-5.2", observed) == "z-ai/glm-5.2"
    assert (
        vscode_lm_bridge.resolve_editor_model_alias("claude-sonnet-current", observed)
        == "anthropic/claude-sonnet-4"
    )
    assert vscode_lm_bridge.resolve_editor_model_alias("deepseek-v4-pro", ["glm-5.2"]) is None


@pytest.mark.parametrize(
    ("adapter_id", "requested_model", "observed_model"),
    [
        (
            runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
            "deepseek-v4-pro",
            "deepseek-chat",
        ),
        (runtime_adapters.GLM_VSCODE_LM_ADAPTER, "glm-5.2", "z-ai/glm-5.2"),
        (
            runtime_adapters.VSCODE_LM_ADAPTER,
            "claude-sonnet-current",
            "anthropic/claude-sonnet-4",
        ),
    ],
)
def test_provider_env_uses_observed_editor_alias_in_bridge_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter_id: str,
    requested_model: str,
    observed_model: str,
) -> None:
    root = _initialized_root(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_readiness(repo: Path, **kwargs: object) -> dict[str, object]:
        calls.append({"repo": repo, **kwargs})
        return {
            "launchable": True,
            "blocker_reason": "",
            "resolved_model": observed_model,
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_readiness
    )
    monkeypatch.setattr(vscode_lm_bridge, "_repo_id", lambda _repo: "repo_test")
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(tmp_path / "bridge"))
    manager = process_launcher.ProcessManager(
        repo=root,
        process_log_path=tmp_path / "process.jsonl",
        isolation_enabled=False,
    )

    provider_env, effective_model = manager._resolve_provider_env(adapter_id, requested_model)
    assert provider_env is None
    assert effective_model == observed_model
    assert calls[0]["model"] == requested_model
    assert calls[0]["adapter_id"] == adapter_id

    request_id = "0123456789abcdef0123456789abcdef"
    workspace_root = tmp_path / request_id
    workspace_path = workspace_root / "worktree"
    workspace_home = workspace_root / "home"
    workspace_path.mkdir(parents=True)
    workspace_home.mkdir()
    request = vscode_lm_bridge.create_request(
        repo=root,
        request_id=request_id,
        workspace_path=workspace_path,
        workspace_home=workspace_home,
        prompt="Implement the focused change",
        model=str(effective_model),
        allowed_writes=["src/aiworkhub/process_launcher.py"],
        timeout_seconds=60,
    )
    payload = json.loads(request.request_path.read_text(encoding="utf-8"))
    assert payload["model"] == observed_model
    assert payload["model"] != requested_model
