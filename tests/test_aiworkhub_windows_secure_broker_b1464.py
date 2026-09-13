from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
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
    task_reconciler,
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
    # The preflight aggregate now consults reconciler authority as its own
    # evidence source, and an UNMEASURED reconciler degrades it on purpose.
    # This fixture is about the Windows sandbox boundary, so hand it a
    # measured, healthy reconciler and leave the subject of the test alone.
    monkeypatch.setattr(
        task_reconciler,
        "reconciler_health",
        lambda _root: {
            "ok": True,
            "running": True,
            "authority_state": "active_owner",
            "active_owner": True,
        },
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
        "outer_validation_authority": True,
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
    assert report["provider_summary"]["excluded_route_count"] == 6


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
    now_epoch = 2_000_000_000.0
    native_adapters = {
        "claude_cli",
        "codex_cli",
        "deepseek_copilot_cli",
        "glm_copilot_cli",
    }

    # Zero-history GLM route: the editor bridge is launchable and nothing has
    # been measured failing, so the route is AVAILABLE and selectable.  It
    # used to be held unavailable until a recent terminal success appeared,
    # which no route on a fresh install can ever produce: unavailable routes
    # are never launched, so they never earn the success that would make them
    # available.  Absence of a success is the unmeasured case, not a measured
    # failure, and only measured failures gate here.
    zero_history = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight=preflight,
        now_epoch=now_epoch,
    )
    glm = next(
        worker for worker in zero_history["workers"]
        if worker["worker_id"] == "glm-5.2"
    )
    assert glm["launch_eligible"] is True
    assert glm["available"] is True
    assert glm["route_health"]["state"] == "closed"
    assert glm["route_health"]["failure_kind"] == ""
    # `readiness_status` quotes preflight's own status verbatim -- one word
    # for one fact, never a second spelling invented on this surface.
    glm_preflight = next(
        row for row in preflight["providers"]
        if row["adapter_id"] == "glm_vscode_lm"
    )
    assert glm["readiness_status"] == glm_preflight["status"]
    # The absence of history is reported as evidence, never as a verdict.
    assert (
        glm["route_observation"]["reason"]
        == repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
    )
    # The point of THIS test is unchanged: on a Windows host with no
    # AppContainer sandbox, no native CLI route may be used at all.
    assert all(
        worker["available"] is False
        for worker in zero_history["workers"]
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
    decision = workforce_catalog.rank_task(root, task, catalog=zero_history)
    assert decision["selected_worker_id"] == "glm-5.2"
    assert decision["selected_adapter_id"] == "glm_vscode_lm"
    assert decision["launch_contract"] == {
        "runner": "glm_5.2",
        "adapter_id": "glm_vscode_lm",
        "model": "glm-5.2",
        "task_id": "T-windows-editor-route",
        "identity_rule": "use_same_runner_for_task_create_and_agent_launch_task",
    }

    # A measured FAILURE is what takes the route away.  Two transient
    # failures inside the cooldown trip the circuit; the native CLI routes
    # stay unavailable throughout, which is what this test exists to hold.
    failing = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[{
            "request_id": f"glm-fail-{index}",
            "task_id": f"T-glm-fail-{index}",
            "adapter_id": "glm_vscode_lm",
            "model": "glm-5.2",
            "state": "launch_failed",
            "runner": "glm_5.2",
            "finished_at": datetime.fromtimestamp(
                now_epoch - 60 - index, tz=timezone.utc
            ).isoformat(),
        } for index in range(2)],
        preflight=preflight,
        now_epoch=now_epoch,
    )
    glm = next(
        worker for worker in failing["workers"]
        if worker["worker_id"] == "glm-5.2"
    )
    assert glm["launch_eligible"] is False
    assert glm["available"] is False
    assert glm["route_health"]["state"] == "open"
    assert glm["readiness_status"] == "route_circuit_open"
    assert all(
        worker["available"] is False
        for worker in failing["workers"]
        if worker["effective_adapter_id"] in native_adapters
    )

    decision = workforce_catalog.rank_task(root, task, catalog=failing)
    assert decision["selected_worker_id"] is None
    assert decision["launch_contract"] is None


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


# ── Measured Windows confinement verdict ───────────────────────────────────
# Every branch below is driven by INJECTION.  This suite runs on Linux, macOS
# and a real Windows runner, and a test that forces a platform name and then
# makes a Windows syscall passes on one and fails on the others.


def _probe(available: bool, detail: str):
    return lambda: SimpleNamespace(available=available, detail=detail)


def test_confinement_report_separates_host_fact_from_unwired_execution_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)

    unavailable = worker_workspace.windows_confinement_report(
        probe=_probe(False, "required Win32 export unavailable")
    )
    assert unavailable["platform_is_windows"] is True
    assert unavailable["host_appcontainer_available"] is False
    assert unavailable["reason"] == "win32_appcontainer_unavailable"
    assert unavailable["host_appcontainer_detail"] == (
        "required Win32 export unavailable"
    )
    assert unavailable["available"] is False

    # A host that CAN build an AppContainer is still refused, and the reason
    # now names AIWorkHub rather than blaming the host.
    capable = worker_workspace.windows_confinement_report(
        probe=_probe(True, "AppContainer APIs resolved.")
    )
    assert capable["host_appcontainer_available"] is True
    assert capable["execution_path_wired"] is False
    assert capable["reason"] == "execution_path_not_wired"
    assert capable["available"] is False


def test_confinement_report_never_claims_a_boundary_it_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)

    report = worker_workspace.windows_confinement_report(
        probe=_probe(True, "AppContainer APIs resolved.")
    )

    # Windows today holds a native CLI worker with a kill-on-close Job Object
    # and nothing else.  The report must say so in as many words rather than
    # letting an unconfined tier read as sandboxed.
    assert report["active_confinement"] == "job_object_lifetime_only"
    assert "filesystem" in report["active_does_not_contain"]
    assert "network" in report["active_does_not_contain"]
    assert "filesystem" not in report["active_contains"]


def test_confinement_report_does_not_describe_windows_on_another_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: False)

    report = worker_workspace.windows_confinement_report()

    assert report["reason"] == "platform_not_windows"
    assert report["available"] is False
    assert report["active_confinement"] == "not_applicable"
    assert report["active_contains"] == ()
    assert report["active_does_not_contain"] == ()


def test_windows_sandbox_refusal_carries_the_measured_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)

    with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
        worker_workspace.select_sandbox_backend()

    identifier, _, measured = str(excinfo.value).partition(":")
    # The identifier is the stable contract every existing caller matches on.
    assert identifier == "windows_appcontainer_sandbox_unavailable"
    # The suffix is measured from the running host, so this asserts that a
    # cause is named -- never which one, which differs between a Windows
    # runner and every other platform this suite runs on.
    assert measured in {
        "win32_appcontainer_unavailable",
        "execution_path_not_wired",
    }


def test_appcontainer_execution_stays_off_until_the_launcher_declares_it() -> None:
    """The last wire, asserted as the single switch it is.

    ``worker_supervisor`` dispatches AppContainer on ``execution_backend``,
    but ``process_launcher`` never writes that key into the supervisor spec,
    so the AppContainer branch is unreachable from production.  Flipping the
    flag without that spec key would report a confinement the runtime does not
    apply, so the flag and the spec key must land together.
    """
    launcher_source = (
        Path(worker_workspace.__file__).with_name("process_launcher.py")
    ).read_text(encoding="utf-8")
    launcher_declares_backend = '"execution_backend"' in launcher_source

    assert (
        worker_workspace.WINDOWS_APPCONTAINER_EXECUTION_WIRED
        is launcher_declares_backend
    )
