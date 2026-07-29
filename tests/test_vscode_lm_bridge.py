from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import repository_state, task_store, vscode_lm_bridge, vscode_lm_worker


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
    )
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
    result = vscode_lm_worker.run(request.worker_spec_path)
    assert result["is_error"] is False
    assert result["changed_paths"] == ["out/result.txt"]
    assert (workspace / "out" / "result.txt").read_text(encoding="utf-8") == "ok\n"


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
