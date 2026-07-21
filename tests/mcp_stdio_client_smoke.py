#!/usr/bin/env python3
"""B109 real out-of-process stdio MCP-client smoke + contract re-freeze (v1).

Extends the B108 in-memory client smoke
(``mcp_client_smoke_contract_freeze.py``) to a REAL out-of-process stdio
transport: it launches the ``aiworkhub`` server as its OWN subprocess over
OS stdio pipes via ``mcp.client.stdio.stdio_client`` +
``mcp.StdioServerParameters`` and drives it through a real ``mcp.ClientSession``
(``initialize`` -> ``tools/list`` -> ``tools/call``) -- the exact path a
VS Code / Claude / Codex stdio MCP client uses. It reuses B108's fingerprint /
normalization / no-write method verbatim so the frozen read-only contract v1 is
proven to still hold across the real pipe.

Checks (all must pass for ``frozen_contract_v1`` true):

  C1  all read-only tools VISIBLE via ``tools/list`` over the stdio pipe;
  C2  all write-gated tools also visible (B108 compatibility subsets);
  C3  the complete 33-tool inventory and every inputSchema are exactly equal
      to the B109 frozen names/fingerprints AND deterministic across two
      independent stdio subprocess sessions;
  C4  no queue/audit writes with ``AIWORKHUB_ALLOW_WRITES`` UNSET: the
      MCP-owned audit state dir (passed to the child via env) is byte-identical
      (and empty) before/after every read-only ``tools/call`` and the parent
      queue stays verify-intact;
  C5  same no-write proof with ``AIWORKHUB_ALLOW_WRITES=1`` in the child;
  C6  STDIO transport actually used: two subprocess sessions initialized and
      listed tools over real OS pipes (out-of-process);
  C7  ONLY the MCP server itself is launched: the child command is a python
      interpreter running ``-m aiworkhub.server`` -- no agent/model
      binary, no shell invocation (stdio_client never uses a shell);
  C8  server.py holds no direct subprocess/exec/fork/shell primitive, the
      child launch gate is forced closed, and no launcher state is created.

This harness NEVER enables writes for real, NEVER launches an agent or model,
makes NO network call, and logs NO secret values. It is isolation-safe: it owns
a private mktemp audit dir (passed to the child via
``AIWORKHUB_AUDIT_LOG_PATH``) so the byte-identity proof cannot be
perturbed by a concurrent worker.

Usage:
    PYTHONPATH=tools/geoai-task-mcp/src AIWORKHUB_REPO=/home/shrek/AIWorkHub \
    python3 tools/geoai-task-mcp/tests/mcp_stdio_client_smoke.py [--out result.json]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import get_default_environment, stdio_client  # noqa: E402

from aiworkhub import cli_adapter_readonly_tool as ro  # noqa: E402
from aiworkhub import core  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402

TASK_ID = "CLAUDE_TASK_MCP_STDIO_SUBPROCESS_CLIENT_SMOKE_B109_V1"
RUNNER = "claude_task_mcp_stdio_smoke_b109"

# --- frozen read-only contract v1 (identical to B108) ---------------------
READONLY_TOOLS: tuple[str, ...] = (
    "aiworkhub_task_health",
    "aiworkhub_task_review_queue",
    "aiworkhub_task_list",
    "aiworkhub_task_show",
    "aiworkhub_task_pending_for_runner",
    "aiworkhub_task_collision_guard",
    "aiworkhub_task_usage_report",
    "aiworkhub_task_audit_log_read",
    "aiworkhub_cli_adapter_plan_readonly",
    "aiworkhub_cli_adapter_audit_summary_readonly",
    "aiworkhub_cli_adapter_report_readonly",
)

WRITE_GATED_TOOLS: tuple[str, ...] = (
    "aiworkhub_task_auto_pickup",
    "aiworkhub_task_mark_review",
    "aiworkhub_task_mark_done",
    "aiworkhub_task_export_jsonl",
)

# Byte-canonical sha256(inputSchema) values for the complete B109-visible tool
# inventory. The original 15 B108 fingerprints are unchanged; the other 18
# entries freeze the rest of the staged 33-tool server surface. Any added,
# removed, renamed, or schema-drifted tool flips frozen_contract_v1 to false.
FROZEN_SCHEMA_FINGERPRINTS: dict[str, str] = {
    "aiworkhub_agent_cancel_task": "d6dc39018324299f9ca0163985b3b7ea846ea681a5b4c8300bfcfca5992132ed",
    "aiworkhub_agent_collect_result": "0f18003c33bbfa39a51bd58df9b7199958f654d48be46b22f135eeaac95ebd7e",
    "aiworkhub_agent_launch_task": "e2df07bb8d3b62ba205ce55a4c6952977bfadcf1b9110a50018383c40264ca8c",
    "aiworkhub_agent_list_processes": "ef824212a6e23c40e901bcfb3681aa45db69dd8c7a2ff2a0afd1e6c3dff974e1",
    "aiworkhub_agent_task_status": "ff9aba2f7412aa53948c8f3ba1a83d9bc2e3a4e1886f54bd1306c64b510f31a7",
    "aiworkhub_cli_adapter_audit_summary_readonly": "6ab96b247924a28d5d793064a61e50c59f46a771c53459ec1197e3acca973fee",
    "aiworkhub_cli_adapter_plan_readonly": "7a79866fe17cd414929fc7e59898311436ce102d826637f7bac3df870cc49c9e",
    "aiworkhub_cli_adapter_report_readonly": "f80f2283b05d851f6fe1a405930efb9f65e39e7f2d501dc70ed5635ab5ca538c",
    "aiworkhub_completion_inbox": "fc015d926f2c5df16a05f81f8da9104398479be855f88a4b503e476e3ddfc5eb",
    "aiworkhub_launch_queue_audit_summary_readonly": "d69e90ba19ecaf47938bb1d5afaadbbffaf8e7cdc3bd2a850c5963b2a76ad4ea",
    "aiworkhub_launch_queue_describe_readonly": "f95498863ef4cd023de90327c99b01a2502ead5e42eb637f57399eff2aa13473",
    "aiworkhub_launch_queue_evaluate_readonly": "edc2a889e2948cda4414a45ca2ea3574bee01c851905d3a49c449ad96f1411ff",
    "aiworkhub_supervisor_loop_status": "53af2da13ad6d2029abdb0832822908eb19c5d343cf8bab25dc6f6d62a9abc41",
    "aiworkhub_task_audit_log_read": "74c684c110e619ef2e3c8c01e59beb938438df93b3c4a0de9c661bc9fd93203d",
    "aiworkhub_task_auto_pickup": "fff78a02b8d8a49a54a9d7c5ea8fabdda817fd3bffdcd12bc560cdc5f7051866",
    "aiworkhub_task_auto_pickup_dryrun": "a4961d4d6e6c8f5b3cc6d81d625f27dd63858b9c7720266cd21fa6115dbf67b4",
    "aiworkhub_task_codex_handoff": "7b19a7cf66e19bcbb075dd793e34c54c7450c8b55d30a450e35e23cdfe3d43bd",
    "aiworkhub_task_codex_handoff_markdown": "880b9c601eab1b1024df04987c95957288ae4e6c8b7e82e15f2dab39a6354492",
    "aiworkhub_task_collision_guard": "acfe0038d2537d852cd25d46d0021a0e38a8227a8ab0f8cce9deaef2b8feaf36",
    "aiworkhub_task_cost_ledger": "e3ca25a6bb80d2f6bb991b99feb194b3c459afaa77bfc7de5ec94055aea62643",
    "aiworkhub_task_export_jsonl": "bef633b8f2ef490b50629465aaad568676cd573cdd12e642875b19d9a3c02579",
    "aiworkhub_task_health": "091219a847dddf8926f5a41e7deb3ad33704df05434e23409bceab171e305aeb",
    "aiworkhub_task_list": "eb3d9dcce2e6679c3f9f77cd0a9b689d6e553a0119d944cf8e9f7dde3170fef9",
    "aiworkhub_task_mark_done": "571fa3749c5713a752de7557b3ad5f5520fc6d0a2c0bc56d0de89632e0b76cd2",
    "aiworkhub_task_mark_review": "f108d09c8f51dab3dfe79808e34fa7df610141b0992c0b4ac7abe660cc016344",
    "aiworkhub_task_pending_for_runner": "37de6b3d912cfa4d5deab2f1ae2c1e03eeac9e552de7678604d4309c33227a13",
    "aiworkhub_task_queue_request": "b53127ff1f63327b53557cee43288f710d3965a9907bf71df1fc5ba5937ee7c5",
    "aiworkhub_task_reject_review": "b63f46acd240e9c485a43898d12dc10a7d9adf63ef60b7884cff1ef7c160e6ac",
    "aiworkhub_task_review_queue": "3db01bcd01ceec23a2caf5d7df6b28c5a452c03ac9a482eeeb5a48c5dd2142fc",
    "aiworkhub_task_review_summarize": "6023df30ec17d4326fcc0e2ddd569b1f98976375c31c4f088f26b56bda0863b9",
    "aiworkhub_task_show": "197d2041187737888044d493380cb4d2a233d2195557a52b6a275cb914977dd0",
    "aiworkhub_task_stale_recovery_recommend": "4f423ac0d8e568417fc7a398abae49c57eda8eafa75b356540b0f9787bd38e70",
    "aiworkhub_task_usage_report": "9aeda83460cd1edb99761473dd1aebbd6ddedfa3f96f02b5e2aeae702425168a",
}

# Read-only tools_call payloads (client path) -- required args supplied.
READONLY_CALL_ARGS: dict[str, dict[str, Any]] = {
    "aiworkhub_task_health": {},
    "aiworkhub_task_review_queue": {},
    "aiworkhub_task_list": {"status": "pending", "limit": 3},
    "aiworkhub_task_show": {"task_id": TASK_ID},
    "aiworkhub_task_pending_for_runner": {"runner": RUNNER},
    "aiworkhub_task_collision_guard": {"print_json": True},
    "aiworkhub_task_usage_report": {},
    "aiworkhub_task_audit_log_read": {"max_entries": 10},
    "aiworkhub_cli_adapter_plan_readonly": {
        "task_id": "B109-PLAN", "runner": "b109_smoke", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["claude", "-p", "hi"],
    },
    "aiworkhub_cli_adapter_audit_summary_readonly": {"max_entries": 10},
    "aiworkhub_cli_adapter_report_readonly": {
        "task_id": "B109-REPORT", "runner": "b109_smoke", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["codex", "exec", "review"],
    },
}

# server.py must contain none of these (defense in depth: the server never
# launches a child of its own; taskctl reads live in core.py by design).
LAUNCH_PATTERNS = (
    "subprocess", "os.system", "os.popen", "os.exec", "os.fork",
    "os.spawn", "Popen(", "shell=" "True", "pty.spawn",  # split literal: no contiguous shell-kwarg token in this source
)

# Tokens that would indicate an agent/model launch rather than the MCP server.
AGENT_MODEL_TOKENS = (
    "claude", "codex", "gpt", "gemini", "llama", "ollama",
    "agent", "model", "chat", "deepseek", "qwen",
)

REQUEST_TIMEOUT = timedelta(seconds=120)


def _canon_fp(schema: Any) -> str:
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _snapshot_dir(d: Path) -> list[tuple[str, int, str]]:
    """Byte-level snapshot (relpath, size, sha256) of every file under d."""
    if not d.exists():
        return []
    return sorted(
        (str(p.relative_to(d)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in d.rglob("*") if p.is_file()
    )


def _server_params(audit_log: Path, allow_writes: bool | None) -> StdioServerParameters:
    """Build stdio params that launch ONLY the MCP server subprocess.

    command = this python interpreter, args = ``-m aiworkhub.server``.
    stdio_client spawns it directly over OS pipes (never a shell). The child
    env is a minimal default env plus the paths the server needs and isolated
    audit/process state paths. ``AIWORKHUB_ALLOW_WRITES`` is set/unset per
    round, while ``AIWORKHUB_ALLOW_LAUNCH`` is always forced closed.
    """
    env = get_default_environment()
    env["PYTHONPATH"] = SRC
    env["AIWORKHUB_REPO"] = str(core.repo_root())
    env["AIWORKHUB_AUDIT_LOG_PATH"] = str(audit_log)
    env[process_launcher.PROCESS_LOG_ENV] = str(audit_log.parent / "process_events.jsonl")
    env[process_launcher.PROCESS_DIR_ENV] = str(audit_log.parent / "processes")
    env[process_launcher.ALLOW_LAUNCH_ENV] = "0"
    env.pop("AIWORKHUB_ALLOW_WRITES", None)
    if allow_writes is True:
        env["AIWORKHUB_ALLOW_WRITES"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "aiworkhub.server"],
        env=env,
        cwd=str(core.repo_root()),
    )


async def _list_tools_via_stdio(audit_log: Path) -> dict[str, Any]:
    """Real out-of-process path: spawn server, initialize, tools/list."""
    params = _server_params(audit_log, allow_writes=False)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as s:
            await s.initialize()
            res = await s.list_tools()
            return {t.name: t.inputSchema for t in res.tools}


async def _run_readonly_round_via_stdio(state_dir: Path, allow_writes: bool) -> dict[str, Any]:
    """Spawn server subprocess, call every read-only tool over the pipe, and
    prove the isolated audit state dir stays byte-identical + empty."""
    audit_log = state_dir / "audit.jsonl"
    before = _snapshot_dir(state_dir)
    verify_before = core.run_taskctl(["verify"]).returncode
    params = _server_params(audit_log, allow_writes=allow_writes)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as s:
            await s.initialize()
            for name in READONLY_TOOLS:
                r = await s.call_tool(
                    name, READONLY_CALL_ARGS[name], read_timeout_seconds=REQUEST_TIMEOUT
                )
                if r.isError:
                    return {"ok": False, "reason": f"tool_error:{name}"}
    after = _snapshot_dir(state_dir)
    verify_after = core.run_taskctl(["verify"]).returncode
    return {
        "ok": True,
        "state_before": before,
        "state_after": after,
        "state_byte_identical": before == after,
        "state_empty": after == [],
        "queue_verify_before_rc": verify_before,
        "queue_verify_after_rc": verify_after,
        "queue_verify_intact": verify_before == 0 and verify_after == 0,
    }


def run_smoke() -> dict[str, Any]:
    """Execute the full out-of-process stdio client smoke + contract re-freeze."""
    checks: dict[str, bool] = {}
    detail: dict[str, Any] = {}
    failing_check: str | None = None

    state_dir = Path(tempfile.mkdtemp(prefix="aiworkhub_b109_stdio_smoke_"))
    audit_log = state_dir / "audit.jsonl"

    try:
        # --- C6/C7 transport + launched-command proof ----------------------
        params = _server_params(audit_log, allow_writes=False)
        launched_command = [params.command, *params.args]
        cmd_lower = " ".join(launched_command).lower()
        is_python = os.path.basename(params.command).lower().startswith("python")
        runs_server_module = params.args[:2] == ["-m", "aiworkhub.server"]
        # server module string is exempt from the agent-token scan; only flag
        # tokens elsewhere in the command line.
        scan = cmd_lower.replace("aiworkhub.server", "")
        agent_hits = sorted({t for t in AGENT_MODEL_TOKENS if t in scan})
        checks["only_mcp_server_launched"] = bool(
            is_python and runs_server_module and not agent_hits
        )
        detail["server_command_normalized"] = ["<python>", *params.args]
        detail["launched_agent_or_model_token_hits"] = agent_hits
        detail["child_launch_gate"] = params.env.get(process_launcher.ALLOW_LAUNCH_ENV)
        detail["transport"] = (
            "mcp.client.stdio.stdio_client + mcp.ClientSession over real OS "
            "stdio pipes (out-of-process subprocess)"
        )

        # --- C1/C2 visibility + C3 schema stability (two stdio sessions) ---
        schemas_a = asyncio.run(_list_tools_via_stdio(audit_log))
        schemas_b = asyncio.run(_list_tools_via_stdio(audit_log))
        checks["stdio_transport_used"] = bool(schemas_a) and bool(schemas_b)

        visible = set(schemas_a)
        visible_b = set(schemas_b)
        frozen_tools = set(FROZEN_SCHEMA_FINGERPRINTS)
        ro_visible = [n for n in READONLY_TOOLS if n in visible]
        wg_visible = [n for n in WRITE_GATED_TOOLS if n in visible]
        checks["readonly_tools_visible"] = set(ro_visible) == set(READONLY_TOOLS)
        checks["write_gated_tools_visible"] = set(wg_visible) == set(WRITE_GATED_TOOLS)
        checks["tool_inventory_matches_frozen"] = (
            visible == frozen_tools and visible_b == frozen_tools
        )
        detail["readonly_tools_visible"] = sorted(ro_visible)
        detail["write_gated_tools_visible"] = sorted(wg_visible)
        detail["tools_visible"] = sorted(visible)
        detail["frozen_tool_names"] = sorted(frozen_tools)
        detail["missing_tools"] = sorted(frozen_tools - visible)
        detail["unexpected_tools"] = sorted(visible - frozen_tools)
        detail["total_tools_visible"] = len(visible)

        cur_fp = {n: _canon_fp(s) for n, s in schemas_a.items()}
        fp_b = {n: _canon_fp(s) for n, s in schemas_b.items()}
        mismatches = sorted(
            n for n in set(cur_fp) | frozen_tools
            if cur_fp.get(n) != FROZEN_SCHEMA_FINGERPRINTS.get(n)
        )
        checks["schema_fingerprints_match_frozen"] = (
            cur_fp == FROZEN_SCHEMA_FINGERPRINTS
        )
        checks["schema_deterministic_across_sessions"] = cur_fp == fp_b
        detail["schema_fingerprint_mismatches"] = mismatches
        detail["current_schema_fingerprints"] = cur_fp
        detail["frozen_schema_fingerprints"] = FROZEN_SCHEMA_FINGERPRINTS

        # --- C4 no-write with ALLOW_WRITES unset in the child --------------
        r_unset = asyncio.run(_run_readonly_round_via_stdio(state_dir, allow_writes=False))
        checks["no_write_allow_unset"] = bool(
            r_unset.get("ok") and r_unset.get("state_byte_identical")
            and r_unset.get("state_empty") and r_unset.get("queue_verify_intact")
        )
        detail["round_allow_unset"] = r_unset

        # --- C5 no-write with ALLOW_WRITES=1 in the child ------------------
        r_set = asyncio.run(_run_readonly_round_via_stdio(state_dir, allow_writes=True))
        checks["no_write_allow_set"] = bool(
            r_set.get("ok") and r_set.get("state_byte_identical")
            and r_set.get("state_empty") and r_set.get("queue_verify_intact")
        )
        detail["round_allow_set"] = r_set

        # --- C8 no direct spawn primitive, launch authority, or side effects
        server_src = (Path(SRC) / "aiworkhub" / "server.py").read_text(encoding="utf-8")
        launch_hits = [p for p in LAUNCH_PATTERNS if p in server_src]
        runtime_launch_enabled = process_launcher.launch_gates_open()
        checks["server_no_launch_code"] = not launch_hits
        checks["launch_gate_forced_closed"] = (
            params.env.get(process_launcher.ALLOW_LAUNCH_ENV) == "0"
            and runtime_launch_enabled is False
        )
        checks["no_launch_side_effects"] = (
            checks["only_mcp_server_launched"]
            and checks["launch_gate_forced_closed"]
            and r_unset.get("state_empty") is True
            and r_set.get("state_empty") is True
        )
        detail["server_launch_pattern_hits"] = launch_hits
        detail["launch_enabled"] = runtime_launch_enabled
        detail["launch_implemented"] = process_launcher.LAUNCH_IMPLEMENTED
        detail["legacy_readonly_launch_enabled"] = ro.launch_enabled()
        detail["legacy_readonly_launch_implemented"] = ro.LAUNCH_IMPLEMENTED
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)

    for name, passed in checks.items():
        if not passed:
            failing_check = name
            break

    frozen = failing_check is None
    return {
        "eval_id": "mcp_stdio_subprocess_client_smoke_b109_v1",
        "task_id": TASK_ID,
        "mode": "mcp_stdio_client_smoke_no_agent_launch",
        "client_path": (
            "mcp.ClientSession over mcp.client.stdio.stdio_client "
            "(real out-of-process OS pipes; initialize/tools_list/tools_call)"
        ),
        "extends": "mcp_client_smoke_contract_freeze_b108_v1",
        "frozen_contract_v1": frozen,
        "failing_check": failing_check,
        "readonly_tool_count": len(READONLY_TOOLS),
        "write_gated_tool_count": len(WRITE_GATED_TOOLS),
        "checks": checks,
        "authority_flags": {
            "contract_frozen": False,
            "process_launch_authority": False,
            "agent_launch_authority": False,
            "write_gate_enabled": False,
            "runtime_authority": False,
            "default_authority": False,
        },
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="write result JSON to this path")
    args = ap.parse_args()
    result = run_smoke()
    text = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["frozen_contract_v1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
