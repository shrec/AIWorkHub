"""NF-2026-00719: Codex worker MCP tools are an explicit, fail-closed contract."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions as instr  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

_CODE_WORKER_REQUIRED = (
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_semantic_edit_prepare",
    "aiworkhub_worker_semantic_edit_apply",
    "aiworkhub_worker_semantic_edit_exception_declare",
    "aiworkhub_worker_validation_run",
    "aiworkhub_worker_validation_output_page",
    "aiworkhub_worker_exit_preflight",
)
_SESSION_MEMORY_KB = (
    "aiworkhub_worker_session_current_state",
    "aiworkhub_worker_ai_memory_search",
    "aiworkhub_worker_ai_memory_get",
    "aiworkhub_worker_ai_memory_related",
    "aiworkhub_worker_kb_search",
    "aiworkhub_worker_kb_get",
    "aiworkhub_worker_kb_related",
)
_QUALITY_REVIEW = (
    "aiworkhub_worker_quality_review_packet_read",
    "aiworkhub_worker_quality_review_submit",
)
_SECRET_MARKERS = ("ANTHROPIC_API_KEY", "COPILOT_PROVIDER_API_KEY", "OPENAI_API_KEY")

def _mute_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *args, **kwargs: None)


def _runtime(tmp_path: Path, **kwargs: object) -> w.WorkerMcpRuntime:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    params = dict(
        home=tmp_path / "home",
        request_id="req_nf719",
        task_id="TASK_NF719",
        runner="codex_cli",
        topic="nf719-codex-worker-mcp-tool-exposure",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=["src/aiworkhub/worker_ai_tools_mcp.py"],
        session_topic="Make Codex worker MCP tool exposure deterministic",
        package_import_root=w.resolve_host_package_import_root(),
    )
    params.update(kwargs)
    return w.generate_worker_mcp_runtime(**params)


def test_generated_codex_toml_exposes_role_required_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    runtime = _runtime(tmp_path)
    parsed = tomllib.loads(runtime.codex_config_toml_path.read_text(encoding="utf-8"))
    server = parsed["mcp_servers"][w.SERVER_NAME]
    enabled = tuple(server["enabled_tools"])
    assert enabled == runtime.codex_tool_names
    assert runtime.tool_names == w.MCP_TOOL_NAMES
    assert runtime.codex_tool_names != runtime.tool_names
    assert runtime.codex_tool_names != ("aiworkhub_worker_exit_preflight",)
    for name in _CODE_WORKER_REQUIRED + _SESSION_MEMORY_KB:
        assert name in enabled
    for name in _QUALITY_REVIEW:
        assert name not in enabled
        assert name in runtime.tool_names
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "aiworkhub.worker_ai_tools_mcp"]
    env = server["env"]
    assert env[w.ENV_REQUEST_ID] == "req_nf719"
    assert env[w.ENV_TASK_ID] == "TASK_NF719"
    assert env[w.ENV_REPO] == str(tmp_path / "repo")
    assert env[w.ENV_AUTHORITY_REPO] == str(tmp_path / "repo")
    blob = runtime.codex_config_toml_path.read_text(encoding="utf-8")
    for marker in _SECRET_MARKERS:
        assert marker not in runtime.env
        assert marker not in blob
        assert marker not in json.dumps(json.loads(
            runtime.claude_mcp_config_path.read_text(encoding="utf-8")
        ))


def test_reviewer_codex_tools_include_quality_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    packet = tmp_path / "review_packet.json"
    runtime = _runtime(tmp_path, quality_review_packet_path=packet)
    parsed = tomllib.loads(runtime.codex_config_toml_path.read_text(encoding="utf-8"))
    enabled = tuple(parsed["mcp_servers"][w.SERVER_NAME]["enabled_tools"])
    assert enabled == runtime.codex_tool_names == w.MCP_TOOL_NAMES
    for name in _QUALITY_REVIEW:
        assert name in enabled


def test_claude_copilot_kilo_shapes_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    runtime = _runtime(tmp_path)
    claude = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    copilot = json.loads(runtime.copilot_mcp_config_path.read_text(encoding="utf-8"))
    assert claude == copilot
    server = claude["mcpServers"][w.SERVER_NAME]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "aiworkhub.worker_ai_tools_mcp"]
    assert "enabled_tools" not in server
    kilo = json.loads(runtime.kilo_config_path.read_text(encoding="utf-8"))
    kilo_server = kilo["mcp"][w.SERVER_NAME]
    assert kilo_server["enabled"] is True
    assert kilo_server["type"] == "local"
    assert kilo_server["command"] == [sys.executable, "-m", "aiworkhub.worker_ai_tools_mcp"]
    assert "enabled_tools" not in kilo_server


def test_incomplete_codex_contract_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    with pytest.raises(w.WorkerToolError, match="source_graph_query"):
        w.require_codex_code_worker_tool_contract(
            ("aiworkhub_worker_semantic_edit_prepare",
             "aiworkhub_worker_semantic_edit_apply",
             "aiworkhub_worker_exit_preflight")
        )
    with pytest.raises(w.WorkerToolError, match="semantic_edit_prepare"):
        w.require_codex_code_worker_tool_contract(
            ("aiworkhub_worker_source_graph_query",
             "aiworkhub_worker_semantic_edit_apply")
        )
    _mute_chmod(monkeypatch)
    monkeypatch.setattr(
        w,
        "resolve_codex_enabled_tools",
        lambda catalog, quality_review_bound=False: ("aiworkhub_worker_exit_preflight",),
    )
    with pytest.raises(w.WorkerToolError, match="codex_code_worker_tool_contract_incomplete"):
        _runtime(tmp_path)
    repo = tmp_path / "auth"
    repo.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req_nf719", repo=repo, path=repo, home=tmp_path / "ws_home",
        allowed_writes=("out.json",), parent_baseline={}, workspace_baseline={},
    )
    with pytest.raises(worker_workspace.WorkspaceError, match="codex_code_worker_tool_contract"):
        worker_workspace.provision_worker_mcp_runtime(
            workspace, request_id="req_nf719", task_id="TASK_NF719",
            runner="codex_cli", topic="nf719", backend="landlock",
            source_graph_targets=[], session_topic="nf719",
        )


def test_codex_instructions_do_not_call_toolsearch() -> None:
    policy = instr.render_worker_runtime_policy("codex_cli")
    assert "ToolSearch" not in policy
    assert "Other hosts skip this" not in policy
    assert "aiworkhub_worker_source_graph_query" in policy
    claude = instr.render_worker_runtime_policy("claude_cli")
    assert "ToolSearch" in claude
    assert instr.WORKER_CLAUDE_TOOL_SCHEMA_QUERY in claude
    assert "Other hosts skip this" not in claude
