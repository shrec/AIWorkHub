from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiworkhub import repo_policy, runtime_adapters, source_graph_daemon, worker_workspace


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _common(monkeypatch, *, graph: dict[str, object]) -> None:
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(repo_policy.source_graph_daemon, "daemon_health", lambda _root: graph)
    monkeypatch.setattr(
        repo_policy.task_store,
        "callback_bridge_health",
        lambda _root: {"ok": True, "backlog_count": 0, "retry_count": 0},
    )


def _ready_graph() -> dict[str, object]:
    return {
        "ok": True,
        "status": source_graph_daemon.STATUS_READY,
        "running": True,
        "registered": True,
        "last_success_at": "2026-08-03T12:00:00+00:00",
        "stale_reason": "",
        "build_revision": "aiworkhub.source_graph.semantic.v5",
        "files_seen": 3,
    }


def test_cli_adapter_is_not_launchable_without_enforceable_sandbox(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _common(monkeypatch, graph=_ready_graph())

    def unavailable() -> str:
        raise worker_workspace.WorkspaceError("secure_sandbox_unavailable")

    monkeypatch.setattr(repo_policy.worker_workspace, "select_sandbox_backend", unavailable)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(adapter_id, "/bin/x", True, ""),
    )
    report = repo_policy.build_preflight(root, adapter_id="claude_cli")
    by_adapter = {item["adapter_id"]: item for item in report["providers"]}
    assert report["ok"] is False
    assert "selected_adapter_not_launchable" in report["errors"]
    assert by_adapter["claude_cli"]["status"] == "sandbox_unavailable"
    assert report["sandbox"]["enforceable"] is False


def test_ready_vscode_lm_route_does_not_require_subprocess_sandbox(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _common(monkeypatch, graph=_ready_graph())
    monkeypatch.setattr(
        repo_policy.worker_workspace,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(worker_workspace.WorkspaceError("unavailable")),
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(adapter_id, "/bin/code", True, ""),
    )
    monkeypatch.setattr(
        repo_policy.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {"launchable": True, "blocker_reason": ""},
    )
    report = repo_policy.build_preflight(root, adapter_id=runtime_adapters.VSCODE_LM_ADAPTER)
    by_adapter = {item["adapter_id"]: item for item in report["providers"]}
    assert "selected_adapter_not_launchable" not in report["errors"]
    assert by_adapter[runtime_adapters.VSCODE_LM_ADAPTER]["launchable"] is True
    assert by_adapter[runtime_adapters.VSCODE_LM_ADAPTER]["sandbox_backend"] == "vscode_lm_in_process"


def test_source_graph_requires_success_identity_not_only_ready_label(monkeypatch, tmp_path):
    root = _root(tmp_path)
    graph = _ready_graph()
    graph["last_success_at"] = ""
    graph["build_revision"] = ""
    _common(monkeypatch, graph=graph)
    monkeypatch.setattr(repo_policy.worker_workspace, "select_sandbox_backend", lambda: "bubblewrap")
    report = repo_policy.build_preflight(root)
    assert "source_graph_not_ready" in report["errors"]
    assert report["source_graph"]["ready_for_code"] is False
