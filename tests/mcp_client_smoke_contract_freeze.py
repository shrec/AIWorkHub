#!/usr/bin/env python3
"""B108 real MCP-client smoke + read-only tool contract freeze (v1).

Drives the aiworkhub FastMCP server through a REAL MCP ``ClientSession``
over in-memory streams (``mcp.shared.memory.create_connected_server_and_client_
session``) -- i.e. the same client path a VS Code / Claude / Codex stdio MCP
client uses (``initialize`` -> ``tools/list`` -> ``tools/call``), not a direct
call into server internals.

It freezes the read-only tool contract v1 by asserting, over the client path:

  C1  all read-only tools are VISIBLE via ``tools/list``;
  C2  all write-gated tools are also visible (contract completeness);
  C3  every tool's inputSchema is a STABLE JSON schema -- byte-canonical
      sha256 matches the frozen fingerprint AND is deterministic across two
      independent client sessions;
  C4  no queue/audit writes occur with ``AIWORKHUB_ALLOW_WRITES`` UNSET:
      the MCP-owned audit state dir is byte-identical (and empty) before/after
      every read-only ``tools/call`` and the parent queue stays verify-intact;
  C5  same no-write proof with ``AIWORKHUB_ALLOW_WRITES=1``;
  C6  NO process launch: no launcher is enabled and server.py holds no
      subprocess/exec/fork/shell launch code.

``frozen_contract_v1`` is emitted true ONLY IF every check passes; otherwise
false with ``failing_check`` naming the first failure. This harness NEVER
enables writes for real, NEVER launches a process/agent, makes NO network
call, and logs NO secret values. It is isolation-safe: it owns a private
mktemp audit dir (overriding ``AIWORKHUB_AUDIT_LOG_PATH``) so the
byte-identity proof can never be perturbed by another worker, and it restores
process env on exit.

Usage:
    PYTHONPATH=tools/geoai-task-mcp/src AIWORKHUB_REPO=/home/shrek/AIWorkHub \
    python3 tools/geoai-task-mcp/tests/mcp_client_smoke_contract_freeze.py \
        [--out result.json]
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
from pathlib import Path
from typing import Any

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session as connect_client,
)

from aiworkhub import cli_adapter_readonly_tool as ro  # noqa: E402
from aiworkhub import core  # noqa: E402
from aiworkhub import server  # noqa: E402


# --- frozen read-only contract v1 -----------------------------------------
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

# Byte-canonical sha256(inputSchema) frozen at B108. A drift in ANY tool's
# input schema flips frozen_contract_v1 to false -> this is the freeze.
FROZEN_SCHEMA_FINGERPRINTS: dict[str, str] = {
    "aiworkhub_cli_adapter_audit_summary_readonly": "6ab96b247924a28d5d793064a61e50c59f46a771c53459ec1197e3acca973fee",
    "aiworkhub_cli_adapter_plan_readonly": "7a79866fe17cd414929fc7e59898311436ce102d826637f7bac3df870cc49c9e",
    "aiworkhub_cli_adapter_report_readonly": "f80f2283b05d851f6fe1a405930efb9f65e39e7f2d501dc70ed5635ab5ca538c",
    "aiworkhub_task_audit_log_read": "74c684c110e619ef2e3c8c01e59beb938438df93b3c4a0de9c661bc9fd93203d",
    "aiworkhub_task_auto_pickup": "fff78a02b8d8a49a54a9d7c5ea8fabdda817fd3bffdcd12bc560cdc5f7051866",
    "aiworkhub_task_collision_guard": "acfe0038d2537d852cd25d46d0021a0e38a8227a8ab0f8cce9deaef2b8feaf36",
    "aiworkhub_task_export_jsonl": "bef633b8f2ef490b50629465aaad568676cd573cdd12e642875b19d9a3c02579",
    "aiworkhub_task_health": "091219a847dddf8926f5a41e7deb3ad33704df05434e23409bceab171e305aeb",
    "aiworkhub_task_list": "eb3d9dcce2e6679c3f9f77cd0a9b689d6e553a0119d944cf8e9f7dde3170fef9",
    "aiworkhub_task_mark_done": "571fa3749c5713a752de7557b3ad5f5520fc6d0a2c0bc56d0de89632e0b76cd2",
    "aiworkhub_task_mark_review": "f108d09c8f51dab3dfe79808e34fa7df610141b0992c0b4ac7abe660cc016344",
    "aiworkhub_task_pending_for_runner": "37de6b3d912cfa4d5deab2f1ae2c1e03eeac9e552de7678604d4309c33227a13",
    "aiworkhub_task_review_queue": "3db01bcd01ceec23a2caf5d7df6b28c5a452c03ac9a482eeeb5a48c5dd2142fc",
    "aiworkhub_task_show": "197d2041187737888044d493380cb4d2a233d2195557a52b6a275cb914977dd0",
    "aiworkhub_task_usage_report": "9aeda83460cd1edb99761473dd1aebbd6ddedfa3f96f02b5e2aeae702425168a",
}

# Read-only tools_call payloads (client path) -- required args supplied.
READONLY_CALL_ARGS: dict[str, dict[str, Any]] = {
    "aiworkhub_task_health": {},
    "aiworkhub_task_review_queue": {},
    "aiworkhub_task_list": {"status": "pending", "limit": 3},
    "aiworkhub_task_show": {"task_id": "CLAUDE_TASK_MCP_CLIENT_SMOKE_CONTRACT_FREEZE_B108_V1"},
    "aiworkhub_task_pending_for_runner": {"runner": "claude_task_mcp_client_smoke_b108"},
    "aiworkhub_task_collision_guard": {"print_json": True},
    "aiworkhub_task_usage_report": {},
    "aiworkhub_task_audit_log_read": {"max_entries": 10},
    "aiworkhub_cli_adapter_plan_readonly": {
        "task_id": "B108-PLAN", "runner": "b108_smoke", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["claude", "-p", "hi"],
    },
    "aiworkhub_cli_adapter_audit_summary_readonly": {"max_entries": 10},
    "aiworkhub_cli_adapter_report_readonly": {
        "task_id": "B108-REPORT", "runner": "b108_smoke", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["codex", "exec", "review"],
    },
}

LAUNCH_PATTERNS = (
    "subprocess", "os.system", "os.popen", "os.exec", "os.fork",
    "os.spawn", "Popen(", "shell=True", "pty.spawn",
)


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


async def _list_tools_via_client() -> dict[str, Any]:
    """Real client path: initialize -> tools/list; return {name: schema}."""
    async with connect_client(server.mcp._mcp_server) as client:
        res = await client.list_tools()
        return {t.name: t.inputSchema for t in res.tools}


async def _run_readonly_round_via_client(state_dir: Path) -> dict[str, Any]:
    """Call every read-only tool over the client; prove state dir unchanged."""
    before = _snapshot_dir(state_dir)
    verify_before = core.run_taskctl(["verify"]).returncode
    async with connect_client(server.mcp._mcp_server) as client:
        for name in READONLY_TOOLS:
            r = await client.call_tool(name, READONLY_CALL_ARGS[name])
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
    """Execute the full client smoke + contract-freeze checks."""
    checks: dict[str, bool] = {}
    detail: dict[str, Any] = {}
    failing_check: str | None = None

    # Isolate the MCP-owned audit state dir so byte-identity cannot be
    # perturbed by a concurrent worker. Restore env on exit.
    prev_audit = os.environ.get("AIWORKHUB_AUDIT_LOG_PATH")
    prev_allow = os.environ.get("AIWORKHUB_ALLOW_WRITES")
    state_dir = Path(tempfile.mkdtemp(prefix="aiworkhub_b108_smoke_"))
    os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = str(state_dir / "audit.jsonl")

    try:
        # --- C1/C2 visibility + C3 schema stability (two client sessions) --
        schemas_a = asyncio.run(_list_tools_via_client())
        schemas_b = asyncio.run(_list_tools_via_client())

        visible = set(schemas_a)
        ro_visible = [n for n in READONLY_TOOLS if n in visible]
        wg_visible = [n for n in WRITE_GATED_TOOLS if n in visible]
        checks["readonly_tools_visible"] = set(ro_visible) == set(READONLY_TOOLS)
        checks["write_gated_tools_visible"] = set(wg_visible) == set(WRITE_GATED_TOOLS)
        detail["readonly_tools_visible"] = sorted(ro_visible)
        detail["write_gated_tools_visible"] = sorted(wg_visible)
        detail["total_tools_visible"] = len(visible)

        cur_fp = {n: _canon_fp(s) for n, s in schemas_a.items()}
        fp_b = {n: _canon_fp(s) for n, s in schemas_b.items()}
        mismatches = sorted(
            n for n in FROZEN_SCHEMA_FINGERPRINTS
            if cur_fp.get(n) != FROZEN_SCHEMA_FINGERPRINTS[n]
        )
        deterministic = cur_fp == fp_b
        checks["schema_fingerprints_match_frozen"] = not mismatches
        checks["schema_deterministic_across_sessions"] = deterministic
        detail["schema_fingerprint_mismatches"] = mismatches
        detail["frozen_schema_fingerprints"] = FROZEN_SCHEMA_FINGERPRINTS

        # --- C4 no-write with ALLOW_WRITES unset ---------------------------
        os.environ.pop("AIWORKHUB_ALLOW_WRITES", None)
        r_unset = asyncio.run(_run_readonly_round_via_client(state_dir))
        checks["no_write_allow_unset"] = bool(
            r_unset.get("ok") and r_unset.get("state_byte_identical")
            and r_unset.get("state_empty") and r_unset.get("queue_verify_intact")
        )
        detail["round_allow_unset"] = r_unset

        # --- C5 no-write with ALLOW_WRITES=1 -------------------------------
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "1"
        r_set = asyncio.run(_run_readonly_round_via_client(state_dir))
        checks["no_write_allow_set"] = bool(
            r_set.get("ok") and r_set.get("state_byte_identical")
            and r_set.get("state_empty") and r_set.get("queue_verify_intact")
        )
        detail["round_allow_set"] = r_set

        # --- C6 no process launch ------------------------------------------
        server_src = (Path(SRC) / "aiworkhub" / "server.py").read_text(encoding="utf-8")
        launch_hits = [p for p in LAUNCH_PATTERNS if p in server_src]
        checks["no_process_launch"] = (
            not launch_hits
            and ro.launch_enabled() is False
            and ro.LAUNCH_IMPLEMENTED is False
        )
        detail["server_launch_pattern_hits"] = launch_hits
        detail["launch_enabled"] = ro.launch_enabled()
        detail["launch_implemented"] = ro.LAUNCH_IMPLEMENTED
    finally:
        # Restore env; drop the private state dir.
        if prev_audit is None:
            os.environ.pop("AIWORKHUB_AUDIT_LOG_PATH", None)
        else:
            os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = prev_audit
        if prev_allow is None:
            os.environ.pop("AIWORKHUB_ALLOW_WRITES", None)
        else:
            os.environ["AIWORKHUB_ALLOW_WRITES"] = prev_allow
        shutil.rmtree(state_dir, ignore_errors=True)

    for name, passed in checks.items():
        if not passed:
            failing_check = name
            break

    frozen = failing_check is None
    return {
        "eval_id": "mcp_client_smoke_contract_freeze_b108_v1",
        "task_id": "CLAUDE_TASK_MCP_CLIENT_SMOKE_CONTRACT_FREEZE_B108_V1",
        "mode": "mcp_client_smoke_contract_freeze_no_launch",
        "client_path": "mcp.ClientSession over in-memory streams (initialize/tools_list/tools_call)",
        "frozen_contract_v1": frozen,
        "failing_check": failing_check,
        "readonly_tool_count": len(READONLY_TOOLS),
        "write_gated_tool_count": len(WRITE_GATED_TOOLS),
        "checks": checks,
        "authority_flags": {
            "contract_frozen": False,
            "process_launch_authority": False,
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
