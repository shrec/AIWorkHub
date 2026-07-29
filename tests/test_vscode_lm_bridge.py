from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import (
    process_launcher,
    repository_state,
    task_store,
    vscode_lm_bridge,
    vscode_lm_worker,
    worker_ai_tools_mcp,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _host(bridge_root: Path, repo_id: str, *, models: list[str]) -> Path:
    path = bridge_root / "hosts" / repo_id / "window_test.json"
    vscode_lm_bridge._atomic_json(  # noqa: SLF001 - contract-level test
        path,
        {
            "schema_id": vscode_lm_bridge.HOST_SCHEMA_ID,
            "repo_id": repo_id,
            "window_id": "window_test",
            "models": models,
        },
    )
    return path


def test_readiness_is_repo_scoped_and_credential_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id

    absent = vscode_lm_bridge.bridge_readiness(repo)
    assert absent["launchable"] is False
    assert absent["credential_required"] is False

    host = _host(root, repo_id, models=["glm-5.2"])
    ready = vscode_lm_bridge.bridge_readiness(repo)
    assert ready["launchable"] is True
    assert ready["window_id"] == "window_test"
    if os.name != "nt":
        assert host.stat().st_mode & 0o077 == 0


def test_worker_applies_only_fully_validated_allowed_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    _host(root, repo_id, models=["glm-5.2"])
    request_id = "a" * 32
    request_root = tmp_path / request_id
    workspace = request_root / "worktree"
    home = request_root / "home"
    workspace.mkdir(parents=True)
    home.mkdir()

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Use Source Graph and implement the bounded output.",
        model="glm-5.2",
        allowed_writes=["out/*.txt"],
        timeout_seconds=30,
        source_graph_request={"mode": "focus", "query": "bounded output", "budget": 32},
    )
    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    assert published["initial_source_graph_request"] == {
        "mode": "focus",
        "query": "bounded output",
        "budget": 32,
    }
    response = {
        "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": repo_id,
        "model": {"id": "glm-5.2"},
        "text": json.dumps(
            {
                "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
                "summary": "implemented",
                "files": [{"path": "out/result.txt", "content": "ok\n"}],
            }
        ),
        "error": "",
    }
    vscode_lm_bridge._atomic_json(request.response_path, response)  # noqa: SLF001
    # The production Landlock/seccomp worker denies fchmod.  The writer must
    # rely on mkstemp's owner-only mode and remain functional without it.
    def _deny_fchmod(_fd: int, _mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(vscode_lm_worker.os, "fchmod", _deny_fchmod)
    result = vscode_lm_worker.run(request.worker_spec_path)
    assert result["is_error"] is False
    assert result["changed_paths"] == ["out/result.txt"]
    assert (workspace / "out" / "result.txt").read_text(encoding="utf-8") == "ok\n"
    if os.name != "nt":
        assert (workspace / "out" / "result.txt").stat().st_mode & 0o077 == 0


def test_worker_rejects_whole_mixed_scope_response_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "b" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="bounded",
        model="glm-5.2",
        allowed_writes=["out/*.txt"],
        timeout_seconds=30,
    )
    vscode_lm_bridge._atomic_json(  # noqa: SLF001
        request.response_path,
        {
            "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
            "request_id": request_id,
            "error": "",
            "text": json.dumps(
                {
                    "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
                    "summary": "bad mixed response",
                    "files": [
                        {"path": "out/good.txt", "content": "must not land"},
                        {"path": "../escape.txt", "content": "bad"},
                    ],
                }
            ),
        },
    )
    with pytest.raises(RuntimeError, match="path_escape"):
        vscode_lm_worker.run(request.worker_spec_path)
    assert not (workspace / "out" / "good.txt").exists()


def test_create_request_rejects_cross_request_workspace(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request_id = "c" * 32
    workspace = tmp_path / "different" / "worktree"
    home = tmp_path / "different" / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    with pytest.raises(vscode_lm_bridge.BridgeError, match="request_mismatch"):
        vscode_lm_bridge.create_request(
            repo=repo,
            request_id=request_id,
            workspace_path=workspace,
            workspace_home=home,
            prompt="bounded",
            model="glm-5.2",
            allowed_writes=[],
            timeout_seconds=30,
        )


def test_packaged_glm_worker_module_is_importable_from_isolated_cwd(tmp_path: Path) -> None:
    package_root = worker_ai_tools_mcp.resolve_host_package_import_root()
    provider_env = process_launcher._glm_vscode_worker_env(None, package_root)  # noqa: SLF001
    env = process_launcher.sanitized_env(
        "glm_vscode_lm",
        home=tmp_path,
        isolated_task_queue_db=True,
        provider_env=provider_env,
    )
    completed = subprocess.run(
        [sys.executable, "-m", "aiworkhub.vscode_lm_worker", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--spec" in completed.stdout


def test_glm_bridge_tool_runs_with_exact_worker_audit_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    process_dir = repo / ".aiworkhub" / "runtime" / "processes"
    process_dir.mkdir(parents=True)
    request_id = "d" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    ledger = home / "audit.jsonl"
    key = home / "audit.key"
    ledger.write_text("", encoding="utf-8")
    key.write_bytes(b"k" * 32)
    metadata_path = process_dir / f"{request_id}.request.json"
    metadata_path.write_text(json.dumps({
        "request_id": request_id,
        "task_id": "GLM_BRIDGE_TEST",
        "runner": "glm52_bridge_test",
        "topic": "bridge_test",
        "adapter_id": "glm_vscode_lm",
        "workspace": {"path": str(workspace)},
        "worker_mcp": {
            "authority_repo": str(repo),
            "source_graph_targets": ["src/aiworkhub"],
            "session_topic": "bounded bridge test",
            "audit_ledger_path": str(ledger),
            "audit_hmac_key_path": str(key),
        },
    }), encoding="utf-8")
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
    )
    event = {
        "request_id": request_id,
        "adapter_id": "glm_vscode_lm",
        "state": "running",
        "metadata_path": str(metadata_path),
    }
    monkeypatch.setattr(manager, "_request_events", lambda _rid: [event])
    observed: dict[str, object] = {}

    def _source(ctx: worker_ai_tools_mcp.WorkerToolContext, **kwargs: object) -> dict[str, object]:
        observed.update({"ctx": ctx, "kwargs": kwargs})
        return {"ok": True, "tool": "source_graph", "hit_count": 1}

    monkeypatch.setattr(worker_ai_tools_mcp, "source_graph_query", _source)
    result = manager.invoke_vscode_lm_worker_tool(
        request_id,
        "aiworkhub_manager_source_graph_query",
        {"mode": "focus", "query": "bridge", "budget": 16},
    )
    ctx = observed["ctx"]
    assert result["ok"] is True
    assert isinstance(ctx, worker_ai_tools_mcp.WorkerToolContext)
    assert ctx.request_id == request_id
    assert ctx.task_id == "GLM_BRIDGE_TEST"
    assert ctx.authority_repo == repo.resolve()
    assert ctx.audit_ledger_path == ledger
    assert observed["kwargs"] == {"mode": "focus", "query": "bridge", "budget": 16}
