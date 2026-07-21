#!/usr/bin/env python3
"""Test: Agent Launcher MVP contract (B02 v1).

Validates the request/response schema contract for the four future MCP
tools (launch_task, task_status, collect_result, cancel_task) defined in
tools/geoai-task-mcp/contracts/agent_launcher_mvp_b02_v1.json.

This is a SCHEMA/DRY-RUN test only:
  * loads the JSON contract and checks required shape (4 tools, request +
    response schema for each, safety model, invariant flags)
  * simulates dry-run request/response pairs using a local, in-process
    reference implementation (no subprocess, no network, no real CLI call)
  * asserts the contract source contains no process-launch/exec/shell code
  * never imports or calls any paid/remote model CLI

Isolation: pure read of a static repo file plus in-memory simulation. No
temp files, no shared state, no network. Safe under parallel execution.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "agent_launcher_mvp_b02_v1.json"
)

REQUIRED_TOOLS = (
    "aiworkhub_agent_launch_task",
    "aiworkhub_agent_task_status",
    "aiworkhub_agent_collect_result",
    "aiworkhub_agent_cancel_task",
)

# Patterns that would indicate real process launch / execution capability.
# None of these may appear anywhere in the contract's raw JSON text.
_FORBIDDEN_SOURCE_PATTERNS = (
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "import subprocess",
    "os.system",
    "os.popen",
    "os.exec",
    "os.fork",
    "os.spawn",
    "Popen(",
    "shell=True",
    "pty.spawn",
    "requests.post",
    "urllib.request",
    "httpx.",
)


def _load_contract() -> dict[str, Any]:
    assert CONTRACT_PATH.exists(), f"contract missing: {CONTRACT_PATH}"
    raw = CONTRACT_PATH.read_text(encoding="utf-8")
    for pat in _FORBIDDEN_SOURCE_PATTERNS:
        assert pat not in raw, f"forbidden launch/network pattern in contract: {pat!r}"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Reference dry-run simulation: pure, in-process, no subprocess/network.
# Mirrors the contract's dry_run_examples so tool authors have a checked
# reference before any server wiring exists.
# ---------------------------------------------------------------------------

_ARGV_TEMPLATES = {
    "claude_cli": ["claude", "-p", "<prompt>", "--cwd", "<repo>"],
    "codex_cli": ["codex", "exec", "<prompt>", "--cwd", "<repo>"],
    "deepseek_manual": [],
}


def sim_launch_task(request: dict[str, Any]) -> dict[str, Any]:
    """Pure simulation: never spawns a process, never opens a socket."""

    adapter_id = request["adapter_id"]
    mode = request.get("mode", "dryrun")
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    if mode != "dryrun":
        return {
            "request_id": request_id,
            "task_id": request["task_id"],
            "adapter_id": adapter_id,
            "decision": "launch_blocked_disabled",
            "would_run_argv": [],
            "launch_enabled": False,
            "launch_implemented": False,
            "shell_invocation": False,
            "audit_ref": "tools/geoai-task-mcp/logs/audit.jsonl",
            "blocked_reason": "mode 'live' not supported by MVP; launch never implemented",
        }
    return {
        "request_id": request_id,
        "task_id": request["task_id"],
        "adapter_id": adapter_id,
        "decision": "launch_planned",
        "would_run_argv": _ARGV_TEMPLATES.get(adapter_id, []),
        "launch_enabled": False,
        "launch_implemented": False,
        "shell_invocation": False,
        "audit_ref": "tools/geoai-task-mcp/logs/audit.jsonl",
        "blocked_reason": None,
    }


def sim_collect_result(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": request.get("request_id"),
        "task_id": request["task_id"],
        "verdict": "not_applicable_mvp",
        "summary": "No process was ever launched for this task in this MVP; nothing to collect.",
        "tests_run": [],
        "changed_paths": [],
        "token_cost_report": None,
        "review_ready": False,
    }


def sim_cancel_task(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": request.get("request_id"),
        "task_id": request["task_id"],
        "decision": "cancel_not_applicable_no_process",
        "note": "No process exists to cancel in this MVP; only a dry-run plan (if any) is cleared.",
    }


def main() -> int:
    contract = _load_contract()

    # --- 1. top-level shape ---
    assert contract["contract_id"] == "agent_launcher_mvp_b02_v1"
    assert contract["task_id"] == "CLAUDE_TASK_MCP_AGENT_LAUNCHER_MVP_B02_V1"
    assert contract["verdict"] == "PASS"

    # --- 2. invariant / safety model: launch never implemented/enabled ---
    inv = contract["invariant"]
    assert inv["launch_implemented"] is False
    assert inv["launch_enabled_by_default"] is False
    assert inv["shell_execution_used"] is False
    assert inv["network_launch_used"] is False
    assert inv["real_agent_spawn_used"] is False
    assert "taskctl.py" in inv["source_of_truth"]

    safety = contract["safety_model"]
    assert safety["local_only"] is True
    assert safety["no_shell_execution"] is True
    assert safety["no_network_launch"] is True
    assert safety["no_secret_logging"] is True

    # --- 3. four required tool schemas present with request+response ---
    tools = contract["tools"]
    for name in REQUIRED_TOOLS:
        assert name in tools, f"missing tool schema: {name}"
        t = tools[name]
        assert "request_schema" in t and isinstance(t["request_schema"], dict)
        assert "response_schema" in t and isinstance(t["response_schema"], dict)
        assert t["request_schema"], f"{name} request_schema must be non-empty"
        assert t["response_schema"], f"{name} response_schema must be non-empty"

    # launch_task must require task_id/runner/topic/adapter_id
    launch_req = tools["aiworkhub_agent_launch_task"]["request_schema"]
    for field in ("task_id", "runner", "topic", "adapter_id"):
        assert launch_req[field]["required"] is True, field

    # launch_task response must carry the always-false safety trio
    launch_resp = tools["aiworkhub_agent_launch_task"]["response_schema"]
    assert launch_resp["launch_enabled"]["const"] is False
    assert launch_resp["launch_implemented"]["const"] is False
    assert launch_resp["shell_invocation"]["const"] is False

    # collect_result must never claim review_ready in the MVP
    collect_resp = tools["aiworkhub_agent_collect_result"]["response_schema"]
    assert collect_resp["review_ready"]["const"] is False

    # --- 4. dry_run_examples exist and are internally consistent ---
    examples = contract["dry_run_examples"]
    assert "launch_task_ok" in examples
    assert "launch_task_live_rejected" in examples
    assert "collect_result_not_applicable" in examples
    assert "cancel_task_no_process" in examples

    ex_ok = examples["launch_task_ok"]
    assert ex_ok["response"]["decision"] == "launch_planned"
    assert ex_ok["response"]["would_run_argv"], "claude_cli plan must carry an argv template"

    ex_live = examples["launch_task_live_rejected"]
    assert ex_live["response"]["decision"] == "launch_blocked_disabled"
    assert ex_live["response"]["would_run_argv"] == []

    # --- 5. reference simulation matches contract examples: no subprocess/network ---
    sim_ok = sim_launch_task(ex_ok["request"])
    assert sim_ok["decision"] == ex_ok["response"]["decision"]
    assert sim_ok["would_run_argv"] == ex_ok["response"]["would_run_argv"]
    assert sim_ok["launch_enabled"] is False
    assert sim_ok["launch_implemented"] is False

    sim_live = sim_launch_task(ex_live["request"])
    assert sim_live["decision"] == "launch_blocked_disabled"
    assert sim_live["would_run_argv"] == []

    sim_result = sim_collect_result(examples["collect_result_not_applicable"]["request"])
    assert sim_result["verdict"] == "not_applicable_mvp"
    assert sim_result["review_ready"] is False

    sim_cancel = sim_cancel_task(examples["cancel_task_no_process"]["request"])
    assert sim_cancel["decision"] == "cancel_not_applicable_no_process"

    # --- 6. deepseek_manual adapter never carries a local argv template ---
    manual_plan = sim_launch_task(
        {"task_id": "TX", "runner": "r", "topic": "t", "adapter_id": "deepseek_manual", "mode": "dryrun"}
    )
    assert manual_plan["would_run_argv"] == [], "manual adapter has no local process to plan"

    # --- 7. no-launch-code marker present ---
    assert "no_launch_code_marker" in contract
    assert "No subprocess" in contract["no_launch_code_marker"] or "schema only" in contract["no_launch_code_marker"]

    print("PASS: test_agent_launcher_mvp_b02_v1")
    print("  4 tool request/response schemas present: OK")
    print("  invariant/safety flags all false/local-only: OK")
    print("  dry_run_examples internally consistent: OK")
    print("  in-process simulation matches examples, no subprocess/network: OK")
    print("  deepseek_manual carries no local argv template: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
