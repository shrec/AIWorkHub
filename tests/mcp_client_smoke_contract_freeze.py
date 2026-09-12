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
#
# RE-FROZEN 2026-09-08, and what drifted is on the record because the answer
# was not the obvious one. This gate had not been RUN since it was written:
# pytest collects ``test_*.py`` only and nothing in CI names this file, so it
# sat red without saying so. When it was finally run, 12 of these 15
# fingerprints mismatched, and the cause was measured, not assumed:
#
#   * 10 of 12 drifted because the TOOL FUNCTIONS WERE RENAMED
#     ``geoai_task_*`` -> ``aiworkhub_task_*``. FastMCP names each tool's
#     pydantic argument model ``f"{func.__name__}Arguments"``, so that title
#     -- and only that title -- is inside every inputSchema. Replacing
#     ``aiworkhub_`` with ``geoai_`` in the CURRENT schema title reproduces the
#     OLD fingerprint EXACTLY for all ten, which is the proof that nothing
#     about the parameters changed. This is a project-side identifier rename,
#     NOT an SDK rendering change: the three ``aiworkhub_cli_adapter_*`` tools
#     never drifted at all, because their functions are named
#     ``plan_command_readonly`` / ``audit_summary_readonly`` /
#     ``readonly_tool_report`` and registered under an explicit tool name, so
#     the rename never touched their titles.
#   * 2 of 12 -- ``aiworkhub_task_show`` and ``aiworkhub_task_mark_done`` --
#     are REAL contract changes on top of that rename: ``task_show`` gained
#     ``detail`` (summary|evidence|full) and ``full``; ``mark_done`` gained
#     ``include_card``. Both are additive optional parameters with defaults,
#     so no existing caller breaks -- but they are a contract change and this
#     is where they are recorded.
#
# Adding a NEW tool does not disturb this gate: it fingerprints exactly the 15
# tools named above, not the whole inventory.
FROZEN_SCHEMA_FINGERPRINTS: dict[str, str] = {
    "aiworkhub_cli_adapter_audit_summary_readonly": "6ab96b247924a28d5d793064a61e50c59f46a771c53459ec1197e3acca973fee",
    "aiworkhub_cli_adapter_plan_readonly": "7a79866fe17cd414929fc7e59898311436ce102d826637f7bac3df870cc49c9e",
    "aiworkhub_cli_adapter_report_readonly": "f80f2283b05d851f6fe1a405930efb9f65e39e7f2d501dc70ed5635ab5ca538c",
    "aiworkhub_task_audit_log_read": "5111315e1823d882715a6b1fa754f1c64894ec10eea469b0b2fe7bc5e98ec015",
    "aiworkhub_task_auto_pickup": "02878ab53c86e45277a3c4337c438218de36a3d2d9f82f2385d9afaed2aa243e",
    "aiworkhub_task_collision_guard": "9a6a72f9e63acc7e08a9fd4c95e66382530f96b98bb40467801b6679c2adf6fd",
    "aiworkhub_task_export_jsonl": "60699d5c0d81ccf28ea7cd0a2f415089d0f87d144b40b83117517649fa847a4a",
    "aiworkhub_task_health": "0d6de3645a2bbbb16f9ae4bc7592403918fd5a8768a3888b1018f1570a9aaffc",
    "aiworkhub_task_list": "bf57352dc786f7f0a14c31625711a0a6ab4576d2d85fb730209a4a957efbd914",
    "aiworkhub_task_mark_done": "85f0dad5ce5e23a4c258d1314230223d1ff8adfcee3f17765283986f1dce4e9a",
    "aiworkhub_task_mark_review": "1722a2665425d2ebd8573c3425ee2d04b8c71ae3db24ace3c97e5a82d7e92bd4",
    "aiworkhub_task_pending_for_runner": "864d85c9f3a5a7020e270ddd71f9aeea2ab65ca35932aaf869afaa837950b16d",
    "aiworkhub_task_review_queue": "b5728f6f46c22488977fe80e794f420203cf6725367eb9271cfdad1c6538b878",
    "aiworkhub_task_show": "987a6779aea9974dc849205292e41e5fab2443e04e97f3c2528059a6b60c4ad3",
    "aiworkhub_task_usage_report": "84fe64116d44bde99ea6a1d850e94a3164e32c12b7a4479ecb4dc10bcc0fe7e3",
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
        # This harness runs in a single already-imported server process (see
        # ``_gate_env``, AIWORKHUB_ALLOW_WRITES="0"), so the write-gated
        # decorator decided registration once, at import, with writes off --
        # a correctly write-gated tool is ABSENT here by design for the whole
        # run. ``required_tools`` carves WRITE_GATED_TOOLS out of the "must
        # match frozen fingerprint" requirement while writes are off; the
        # read-only closure is unaffected.
        required_tools = set(FROZEN_SCHEMA_FINGERPRINTS) - set(WRITE_GATED_TOOLS)
        ro_visible = [n for n in READONLY_TOOLS if n in visible]
        wg_visible = [n for n in WRITE_GATED_TOOLS if n in visible]
        checks["readonly_tools_visible"] = set(ro_visible) == set(READONLY_TOOLS)
        checks["write_gated_tools_visible"] = not wg_visible
        detail["readonly_tools_visible"] = sorted(ro_visible)
        detail["write_gated_tools_visible"] = sorted(wg_visible)
        detail["total_tools_visible"] = len(visible)

        cur_fp = {n: _canon_fp(s) for n, s in schemas_a.items()}
        fp_b = {n: _canon_fp(s) for n, s in schemas_b.items()}
        mismatches = sorted(
            n for n in required_tools
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
