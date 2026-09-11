"""Deterministic fake-Windows proof for the NF-2026-00452 supervisor spec."""

from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import aiworkhub.process_launcher as process_launcher
import aiworkhub.process_launcher_launch_isolated as module

CANONICAL_REPO_ID = "repo_57de971f505d4a50a7729a99c32615de"


def test_fake_windows_spec_carries_backend_and_repo_id() -> None:
    spec = module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind="glm53",
        platform="win32",
    )
    assert spec == {
        "backend": "windows_appcontainer",
        "repo_id": CANONICAL_REPO_ID,
        "worker_kind": "glm53",
    }


def test_fake_windows_spec_normalizes_native_worker_kind() -> None:
    spec = module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind="GLM-53 Native",
        platform="win32",
    )
    assert spec["worker_kind"] == "glm53_native"


def test_non_windows_identity_omits_appcontainer_backend() -> None:
    spec = module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind="glm53",
        platform="linux",
    )
    assert spec == {
        "repo_id": CANONICAL_REPO_ID,
        "worker_kind": "glm53",
    }


def test_missing_repo_id_fails_before_spawn() -> None:
    with pytest.raises(ValueError, match="repo_id"):
        module._appcontainer_supervisor_identity(
            repo_id="",
            worker_kind="glm53",
            platform="win32",
        )


class _FakeManager:
    """Minimal ProcessManager stand-in covering only the seams
    launch_isolated reaches before it would hand a process to the OS."""

    def __init__(self, tmp_path: Path, *, repo: str) -> None:
        self.repo = repo
        self.process_dir = tmp_path / "process"
        self._lock = threading.Lock()
        self._live: dict[str, object] = {}

    def _preflight_card(self, task_id, runner, topic, adapter_id, **_kwargs):
        return {}

    def _with_dependency_inputs(self, card):
        return card

    def _resolve_provider_env(self, adapter_id, model):
        return {}, model

    def _launch_reservation(self, _payload):
        return contextlib.nullcontext()

    def _build_adapter(self, **_kwargs):
        return SimpleNamespace(launchable=True, argv=["fake-worker"], reason="")

    def _terminal_authority_grant_path(self, request_id):
        return self.process_dir / f"{request_id}.authority.json"

    def _terminal_authority_key(self):
        return "fake-authority-key"

    def _popen(self, *_args, **_kwargs):
        return SimpleNamespace(pid=4242)

    def _append_event(self, _event):
        return {"state": "running"}

    def _monitor(self, _live):
        return None


def _patch_launch_seams(monkeypatch, tmp_path, *, sandbox_backend, canonical_repo_id):
    def _set(name, value):
        monkeypatch.setattr(process_launcher, name, value, raising=True)

    _set("launch_gates_open", lambda: True)
    _set("_validate_adapter_identity", lambda runner, adapter_id: None)
    _set("_enforce_quality_review_launch_binding", lambda topic, binding: None)
    _set("_validation_only_replay_authorization", lambda card, task_id: None)
    _set(
        "validate_workforce_identity",
        lambda runner, adapter_id, model, risk_tier=None, repo=None: model,
    )
    _set("_memory_launch_admission", lambda: {"admit": True})
    _set("_external_readonly_dirs", lambda card, adapter_id: [])
    _set("_task_authority_repo", lambda repo, card: "authority-repo")
    _set("_launch_project_context", lambda repo, card, binding: None)
    _set("_sandbox_backend_for_adapter", lambda adapter_id: sandbox_backend)
    _set("_VSCODE_LM_IN_PROCESS_ADAPTERS", frozenset())
    _set("_touch_0600", lambda path: None)
    _set("chmod_path", lambda path, mode: None)
    _set("_worker_mcp_source_graph_targets", lambda context_result: [])
    _set("_worker_mcp_session_topic", lambda context_result, topic: "session-topic")
    _set(
        "_provision_worker_mcp_runtime_for_authority",
        lambda workspace, **_kwargs: SimpleNamespace(
            server_name="worker-mcp",
            tool_names=[],
            audit_ledger_path=tmp_path / "ledger.json",
            audit_hmac_key_path=tmp_path / "hmac.key",
            claude_mcp_config_path=tmp_path / "claude.json",
            copilot_mcp_config_path=tmp_path / "copilot.json",
            codex_config_toml_path=tmp_path / "codex.toml",
            kilo_config_path=tmp_path / "kilo.json",
            package_import_root=tmp_path,
        ),
    )
    _set("_launch_source_graph_request", lambda card, binding: None)
    _set("build_worker_prompt", lambda **_kwargs: "prompt-text")
    _set(
        "create_workspace",
        lambda repo, request_id, card, adapter_id: SimpleNamespace(
            repo=repo,
            path=tmp_path / "workspace",
            home=tmp_path / "workspace-home",
            allowed_writes=[],
            parent_baseline=None,
            as_metadata=lambda: {},
        ),
    )
    _set("build_residual_contract_manifest", lambda workspace, card: [])
    _set(
        "_materialize_worker_rework_overlay",
        lambda workspace, *, task_id, card: (None, None),
    )
    _set(
        "_materialize_crash_retry_packet",
        lambda process_dir, workspace, *, task_id, card, rework_overlay_packet: (
            None,
            None,
        ),
    )
    _set("worker_launch_env", lambda adapter_id, **_kwargs: {})
    _set(
        "sandbox_argv",
        lambda workspace, adapter_id, argv, *, backend, package_import_root: list(
            argv
        ),
    )
    _set("_worker_launch_cwd", lambda path: str(path))
    _set("_path_manifest", lambda repo, paths: {})
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "resolve_host_package_import_root",
        lambda: tmp_path,
        raising=True,
    )
    monkeypatch.setattr(
        process_launcher.core, "_lifecycle_state", lambda card: "queued", raising=True
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "claim_start_exact",
        lambda *_args, **_kwargs: {"ok": True},
        raising=True,
    )
    _set(
        "_committed_claim_card",
        lambda claim, *, request_id, task_id, runner, topic: {"claim_epoch": 1},
    )
    _set("_project_context_delivery", lambda context_result, prompt_hash: {})
    _set("_write_terminal_authority_grant", lambda *_args, **_kwargs: None)
    _set("_worker_supervisor_script", lambda: tmp_path / "supervisor_script.py")
    _set("_pid_start_ticks", lambda pid: 999)
    monkeypatch.setattr(
        process_launcher.project_context.repository_state,
        "inspect_repository",
        lambda repo: SimpleNamespace(
            manifest=SimpleNamespace(repo_id=canonical_repo_id)
        ),
        raising=True,
    )


def _run_launch_isolated(
    monkeypatch,
    tmp_path,
    *,
    adapter_id,
    sandbox_backend,
    canonical_repo_id=CANONICAL_REPO_ID,
):
    _patch_launch_seams(
        monkeypatch,
        tmp_path,
        sandbox_backend=sandbox_backend,
        canonical_repo_id=canonical_repo_id,
    )
    spec_writes: list[tuple[object, dict]] = []

    def _record_write(path, payload):
        spec_writes.append((path, payload))

    monkeypatch.setattr(process_launcher, "write_json_0600", _record_write, raising=True)

    manager = _FakeManager(tmp_path, repo="repo_self_unused")
    result = module.launch_isolated(
        manager,
        task_id="task-1",
        runner="runner-1",
        topic="topic-1",
        adapter_id=adapter_id,
        model=None,
        owner_prompt="do the thing",
        timeout_seconds=60,
    )
    assert result["ok"] is True, result
    spec_payload = next(
        payload
        for path, payload in spec_writes
        if str(path).endswith("supervisor-spec.json")
    )
    return spec_payload


def test_launch_isolated_windows_appcontainer_spec_carries_identity(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    spec = _run_launch_isolated(
        monkeypatch,
        tmp_path,
        adapter_id="GLM-53 Native",
        sandbox_backend="windows_appcontainer",
    )
    assert spec["execution_backend"] == "windows_appcontainer"
    assert spec["repo_id"] == CANONICAL_REPO_ID
    assert spec["worker_kind"] == "glm53_native"


def test_launch_isolated_non_appcontainer_spec_omits_identity_keys(
    monkeypatch, tmp_path
) -> None:
    spec = _run_launch_isolated(
        monkeypatch,
        tmp_path,
        adapter_id="glm53",
        sandbox_backend="landlock",
    )
    assert "execution_backend" not in spec
    assert "repo_id" not in spec
    assert "worker_kind" not in spec
