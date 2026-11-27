"""NF-2026-00993 / NF-2026-00994: OpenCode workers must see their worker MCP tools.

* An OpenCode-shaped stdio handshake (initialize with each initialize-era
  protocolVersion OpenCode's MCP client sends, notifications/initialized,
  tools/list) against the real worker MCP server on its stdlib backend returns
  a non-empty tool list in exactly the ListToolsResult shape that revision
  defines.  Before the fix the reply also carried the 2026-07-28 envelope
  (resultType/ttlMs/cacheScope).
* build_opencode_worker_mcp_config matches the pinned OpenCode config shape,
  and its builder cites the schema source.
* An OpenCode turn that never reached a worker MCP tool lands as
  launch_failed:worker_mcp_tools_unavailable (infra), never as a model turn.
* Every AIWorkHub tool name in the rendered OpenCode worker prompt is one the
  adapter exposes (awh_<tool>); the Claude worker prompt is unchanged.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    agent_tool_instructions,
    process_launcher,
    runtime_adapters,
    terminal_failure_classification,
    worker_ai_tools_mcp,
)

# The initialize-era revisions OpenCode's MCP client negotiates.
OPENCODE_CLIENT_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
# ListToolsResult / Tool members those revisions define (2025-06-18 superset).
_LIST_TOOLS_RESULT_KEYS = {"tools", "nextCursor", "_meta"}
_TOOL_KEYS = {"name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta"}
# Hosted Linux runs the packaged runtime without the optional ``mcp`` package,
# so the worker server is the stdlib FallbackFastMCP.  Force that backend.
_FALLBACK_WORKER_MAIN = (
    "import runpy, sys\n"
    "sys.modules['mcp'] = None\n"
    "runpy.run_module('aiworkhub.worker_ai_tools_mcp', run_name='__main__')\n"
)


def _worker_env(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "worktree"
    authority = tmp_path / "authority"
    repo.mkdir(exist_ok=True)
    authority.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("AIWORKHUB_REPO_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))
    env.update(
        {
            worker_ai_tools_mcp.ENV_TASK_ID: "T-NF00993",
            worker_ai_tools_mcp.ENV_RUNNER: "opencode_gpt-6-luna",
            worker_ai_tools_mcp.ENV_TOPIC: "opencode-tools",
            worker_ai_tools_mcp.ENV_REQUEST_ID: "R-NF00993",
            worker_ai_tools_mcp.ENV_REPO: str(repo),
            worker_ai_tools_mcp.ENV_AUTHORITY_REPO: str(authority),
        }
    )
    return env


def _opencode_handshake(tmp_path: Path, version: str) -> dict[int, dict[str, Any]]:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "opencode", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ]
    completed = subprocess.run(
        [sys.executable, "-c", _FALLBACK_WORKER_MAIN],
        input="".join(json.dumps(request) + "\n" for request in requests).encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        env=_worker_env(tmp_path),
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")[-2000:]
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert all("id" in reply for reply in replies), "a notification must get no reply"
    return {reply["id"]: reply for reply in replies}


@pytest.mark.parametrize("version", OPENCODE_CLIENT_PROTOCOL_VERSIONS)
def test_opencode_handshake_lists_worker_tools_in_the_negotiated_shape(tmp_path, version):
    replies = _opencode_handshake(tmp_path, version)
    assert set(replies) == {0, 1}
    initialize = replies[0]["result"]
    assert initialize["protocolVersion"] == version
    assert "tools" in initialize["capabilities"]
    listed = replies[1]["result"]
    assert "error" not in replies[1]
    # Exactly the revision's ListToolsResult: no 2026-07-28 envelope fields.
    assert set(listed) <= _LIST_TOOLS_RESULT_KEYS, sorted(set(listed) - _LIST_TOOLS_RESULT_KEYS)
    assert "nextCursor" not in listed, "the whole list fits one page"
    tools = listed["tools"]
    assert tools, "an OpenCode worker must never see an empty worker tool list"
    names = {tool["name"] for tool in tools}
    for tool in tools:
        assert set(tool) <= _TOOL_KEYS, (tool["name"], sorted(set(tool) - _TOOL_KEYS))
        assert tool["inputSchema"]["type"] == "object"
    # Every tool the OpenCode permission contract allows is actually served.
    assert set(runtime_adapters.OPENCODE_WORKER_MCP_TOOLS) <= names


def test_opencode_worker_config_matches_the_pinned_schema_shape():
    command = ("/usr/bin/python3", "-m", "aiworkhub.worker_ai_tools_mcp")
    environment = {
        "AIWORKHUB_WORKER_MCP_TASK_ID": "T-1",
        "AIWORKHUB_WORKER_MCP_REPO": "/work/R-1/worktree",
    }
    config = runtime_adapters.build_opencode_worker_mcp_config(command, environment=environment)
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "permission": runtime_adapters.opencode_worker_permission_contract(),
        "mcp": {
            "servers": {
                "awh": {
                    "type": "local",
                    "command": list(command),
                    "codemode": False,
                    "environment": environment,
                }
            }
        },
    }
    assert runtime_adapters.validate_opencode_worker_config(config) is config
    # The builder cites where this shape comes from.
    source = inspect.getsource(runtime_adapters)
    builder_at = source.index("def _build_opencode_mcp_config(")
    preamble = source[max(0, builder_at - 1200):builder_at]
    assert "Schema source" in preamble and "https://opencode.ai/config.json" in preamble


# -- fail closed: no worker MCP tools is an infra launch failure ---------------


def _opencode_metadata(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "adapter_id": runtime_adapters.OPENCODE_CLI_ADAPTER,
        "task_id": "T-NF00993",
        "runner": "opencode_gpt-6-luna",
        "topic": "opencode-tools",
        "worker_mcp": {
            "audit_ledger_path": str(tmp_path / "audit.jsonl"),
            "audit_hmac_key_path": str(tmp_path / "audit.key"),
        },
    }
    metadata.update(overrides)
    return metadata


def _stub_ledger(
    monkeypatch, verified: int, provenance: dict[str, int], *, ok: bool = True
) -> None:
    def verify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": ok, "entries_verified": verified, "provenance_counts": provenance}

    monkeypatch.setattr(worker_ai_tools_mcp, "verify_audit_ledger", verify)


_TOOLS_UNAVAILABLE = terminal_failure_classification.WORKER_MCP_TOOLS_UNAVAILABLE
_UNVERIFIABLE = (
    f"{_TOOLS_UNAVAILABLE}:"
    f"{terminal_failure_classification.AUDIT_LEDGER_UNVERIFIABLE}"
)


def test_opencode_turn_without_worker_mcp_calls_is_launch_failed_infra(tmp_path, monkeypatch):
    for verified, provenance in ((0, {}), (2, {"prefetch": 2})):
        _stub_ledger(monkeypatch, verified, provenance)
        reason = runtime_adapters.opencode_worker_mcp_tools_unavailable(
            _opencode_metadata(tmp_path), "R-NF00993", "exited"
        )
        assert reason == f"{_TOOLS_UNAVAILABLE}:worker_mcp_calls=0"


def test_provider_nonzero_exit_keeps_its_own_worker_failed_verdict(tmp_path, monkeypatch):
    # A provider that exited non-zero is already a failure with stronger evidence
    # of its own (NF-2026-01493); the tool-list rule only re-files a clean exit.
    _stub_ledger(monkeypatch, 0, {})
    metadata = _opencode_metadata(tmp_path)
    assert runtime_adapters.opencode_worker_mcp_tools_unavailable(
        metadata, "R-NF00993", "worker_failed"
    ) == ""
    assert runtime_adapters.opencode_worker_mcp_tools_gate(
        metadata, "R-NF00993", "worker_failed", "boom", None, None
    ) == ("worker_failed", "boom", None)


def test_missing_or_unreadable_ledger_fails_closed(tmp_path):
    # Real verifier, no ledger or key on disk: absent evidence is never a pass.
    reason = runtime_adapters.opencode_worker_mcp_tools_unavailable(
        _opencode_metadata(tmp_path), "R-NF00993", "exited"
    )
    assert reason == _UNVERIFIABLE
    unprovisioned = runtime_adapters.opencode_worker_mcp_tools_unavailable(
        _opencode_metadata(tmp_path, worker_mcp={}), "R-NF00993", "exited"
    )
    assert unprovisioned == f"{_TOOLS_UNAVAILABLE}:worker_mcp_runtime_not_provisioned"


def test_reason_constant_has_one_owner_and_is_recognised_as_infra():
    # runtime_adapters reads the vocabulary from its owner; it never restates the
    # literal, and never imports the classifier at module level (the vendored
    # worker MCP runtime imports runtime_adapters without that closure).
    adapters_source = inspect.getsource(runtime_adapters)
    for literal in (
        _TOOLS_UNAVAILABLE,
        terminal_failure_classification.WORKER_MCP_RUNTIME_NOT_PROVISIONED,
        terminal_failure_classification.AUDIT_LEDGER_UNVERIFIABLE,
    ):
        assert f'"{literal}"' not in adapters_source, literal
    assert "\nfrom .terminal_failure_classification import" not in adapters_source
    assert "\nfrom . import terminal_failure_classification" not in adapters_source
    for text, detail in (
        ("worker_mcp_tools_unavailable:worker_mcp_calls=0", (("worker_mcp_calls", 0),)),
        (_UNVERIFIABLE, (("", "audit_ledger_unverifiable"),)),
        (
            "worker_mcp_tools_unavailable:worker_mcp_runtime_not_provisioned",
            (("", "worker_mcp_runtime_not_provisioned"),),
        ),
    ):
        recognised = terminal_failure_classification.recognised_reason(text)
        assert recognised is not None, text
        assert recognised.code == "worker_mcp_tools_unavailable"
        assert recognised.detail == detail
        assert recognised.render() == text
    code = terminal_failure_classification.WORKER_MCP_TOOLS_UNAVAILABLE
    disposition = terminal_failure_classification.REASON_DISPOSITION[code]
    assert disposition == terminal_failure_classification.FAILURE_CLASS_TRANSIENT
    cause = terminal_failure_classification._REASON_CAUSE[code]
    assert cause != terminal_failure_classification.CAUSE_CANDIDATE_CODE
    category = terminal_failure_classification._CAUSE_FAILURE_CATEGORY[cause]
    assert category != terminal_failure_classification.FAILURE_CATEGORY_CANDIDATE_CODE
    assert terminal_failure_classification._CAUSE_FAILURE_CLASS[cause] == disposition
    assert code not in terminal_failure_classification.unclassified_reason_constants()


def test_unverified_ledger_counts_are_not_evidence(tmp_path, monkeypatch):
    # A ledger whose HMAC chain did not verify proves nothing, whatever it counts.
    _stub_ledger(monkeypatch, 5, {"prefetch": 0}, ok=False)
    metadata = _opencode_metadata(tmp_path)
    assert runtime_adapters.opencode_worker_mcp_tools_unavailable(
        metadata, "R-NF00993", "exited"
    ) == _UNVERIFIABLE
    state, error, reason = runtime_adapters.opencode_worker_mcp_tools_gate(
        metadata, "R-NF00993", "exited", None, None, None
    )
    assert (state, error) == ("launch_failed", _UNVERIFIABLE)
    assert reason is not None and reason.code == "worker_mcp_tools_unavailable"


@pytest.mark.parametrize("exc", [OSError("ledger vanished"), ValueError("bad ledger")])
def test_verifier_exception_fails_closed_without_raising(tmp_path, monkeypatch, exc):
    def verify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise exc

    monkeypatch.setattr(worker_ai_tools_mcp, "verify_audit_ledger", verify)
    metadata = _opencode_metadata(tmp_path)
    assert runtime_adapters.opencode_worker_mcp_tools_unavailable(
        metadata, "R-NF00993", "exited"
    ) == _UNVERIFIABLE
    state, error, reason = runtime_adapters.opencode_worker_mcp_tools_gate(
        metadata, "R-NF00993", "exited", None, None, None
    )
    assert (state, error) == ("launch_failed", _UNVERIFIABLE)
    assert reason is not None


def test_worker_that_reached_its_tools_or_other_adapters_are_untouched(tmp_path, monkeypatch):
    _stub_ledger(monkeypatch, 3, {"prefetch": 1, "live": 2})
    assert runtime_adapters.opencode_worker_mcp_tools_unavailable(
        _opencode_metadata(tmp_path), "R-NF00993", "exited"
    ) == ""
    _stub_ledger(monkeypatch, 0, {})
    for metadata, state in (
        (_opencode_metadata(tmp_path, adapter_id="claude_cli"), "exited"),
        (_opencode_metadata(tmp_path), "timed_out"),
        (_opencode_metadata(tmp_path), "cancelled"),
        (_opencode_metadata(tmp_path, execution_mode="validation_only_replay"), "exited"),
    ):
        assert runtime_adapters.opencode_worker_mcp_tools_unavailable(
            metadata, "R-NF00993", state
        ) == ""


def test_gate_turns_a_toolless_opencode_turn_into_launch_failed(tmp_path, monkeypatch):
    _stub_ledger(monkeypatch, 0, {})
    metadata = _opencode_metadata(tmp_path)
    state, error, reason = runtime_adapters.opencode_worker_mcp_tools_gate(
        metadata, "R-NF00993", "exited", None, None, None
    )
    assert state == "launch_failed"
    assert error == f"{_TOOLS_UNAVAILABLE}:worker_mcp_calls=0"
    # The typed reason is really minted, not None == None.
    assert reason is not None
    assert reason.code == terminal_failure_classification.WORKER_MCP_TOOLS_UNAVAILABLE
    assert reason.render() == error
    # A provider-sealed failure keeps its own verdict; a tool-using turn is untouched.
    sealed = {"reason": "provider_refused"}
    assert runtime_adapters.opencode_worker_mcp_tools_gate(
        metadata, "R-NF00993", "launch_failed", "provider_refused", "r", sealed
    ) == ("launch_failed", "provider_refused", "r")
    # An empty sealed mapping (``launch_failure or route_failure`` == {}) is not
    # a sealed failure and must not bypass the rule.
    for empty in ({}, "", 0):
        state, error, reason = runtime_adapters.opencode_worker_mcp_tools_gate(
            metadata, "R-NF00993", "exited", None, None, empty
        )
        assert state == "launch_failed", empty
        assert error == f"{_TOOLS_UNAVAILABLE}:worker_mcp_calls=0"
        assert reason is not None
    _stub_ledger(monkeypatch, 2, {"live": 2})
    assert runtime_adapters.opencode_worker_mcp_tools_gate(
        metadata, "R-NF00993", "exited", None, None, None
    ) == ("exited", None, None)


def test_finalizer_routes_the_reason_to_launch_failed():
    source = inspect.getsource(process_launcher)
    call_at = source.index(
        "terminal_state, error, reason = runtime_adapters.opencode_worker_mcp_tools_gate("
    )
    window = source[call_at:call_at + 300]
    assert "provider_sealed_failure" in window
    # Classified before the failure disposition, so it never reaches review.
    assert call_at < source.index(
        "terminal_disposition = terminal_failure_classification.failure_disposition_from_paths("
    )


# -- tool names: one adapter mapping --------------------------------------------

_ANY_AIWORKHUB_TOOL = re.compile(r"[A-Za-z0-9_]*aiworkhub_[A-Za-z0-9_]+")
_CARD = {"objective": "Fix one bug.", "allowed_writes": ["src/x.py"], "validation": []}


def _prompt(adapter_id: str) -> str:
    return process_launcher.build_worker_prompt(
        task_id="T-NF00993",
        runner="opencode_gpt-6-luna",
        topic="opencode-tools",
        request_id="R-NF00993",
        card=dict(_CARD),
        adapter_id=adapter_id,
    )


def test_opencode_prompt_names_only_tools_the_adapter_exposes():
    exposed = runtime_adapters.adapter_exposed_worker_tool_names(
        runtime_adapters.OPENCODE_CLI_ADAPTER
    )
    assert exposed == {
        f"{runtime_adapters.OPENCODE_WORKER_MCP_SERVER}_{tool}"
        for tool in runtime_adapters.OPENCODE_WORKER_MCP_TOOLS
    }
    named = set(_ANY_AIWORKHUB_TOOL.findall(_prompt(runtime_adapters.OPENCODE_CLI_ADAPTER)))
    assert named, "the OpenCode worker prompt must name its worker tools"
    assert named <= exposed, sorted(named - exposed)
    assert all(name.startswith("awh_") for name in named)


def test_claude_prompt_tool_names_are_unchanged():
    prompt = _prompt("claude_cli")
    policy = agent_tool_instructions.render_worker_runtime_policy("claude_cli", card=dict(_CARD))
    assert prompt.startswith(policy)
    assert "awh_" not in prompt
    assert runtime_adapters.render_adapter_tool_names("claude_cli", policy) == policy
    assert runtime_adapters.adapter_exposed_worker_tool_names("claude_cli") is None


def test_mapping_is_derived_from_the_config_server_key(monkeypatch):
    monkeypatch.setattr(runtime_adapters, "OPENCODE_WORKER_MCP_SERVER", "zz")
    text = "call aiworkhub_worker_source_graph_query, then aiworkhub_unknown_tool."
    rendered = runtime_adapters.render_adapter_tool_names(
        runtime_adapters.OPENCODE_CLI_ADAPTER, text
    )
    assert rendered == "call zz_aiworkhub_worker_source_graph_query, then aiworkhub_unknown_tool."
    # Already-qualified names are never double-prefixed.
    assert runtime_adapters.render_adapter_tool_names(
        runtime_adapters.OPENCODE_CLI_ADAPTER, rendered
    ) == rendered
