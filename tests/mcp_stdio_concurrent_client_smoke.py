#!/usr/bin/env python3
"""B110 concurrent out-of-process stdio MCP-client smoke (v1).

Extends the B109 real-stdio single-session smoke
(``mcp_stdio_client_smoke.py``) to prove the frozen read-only tool contract
holds under CONCURRENCY: it opens TWO independent ``stdio_client`` subprocess
sessions to the ``aiworkhub`` server *at the same time* (overlapping
lifetimes, forced by an ``asyncio.Barrier(2)``) and drives read-only
``tools/call`` from BOTH concurrently. Each session is a separate OS subprocess
with its own OS stdio pipes and its own isolated audit-state dir.

Novel proof over B109 (which ran two sessions sequentially):

  * two server subprocesses are provably ALIVE simultaneously (barrier release
    only happens when BOTH sessions have initialized) -> the contract is proven
    under real concurrency, not one-at-a-time;
  * NO cross-session state bleed: each session gets a DISTINCT runner identity
    and a DISTINCT private audit dir. A session's ``pending_for_runner`` result
    echoes its OWN runner and never the other session's (application-layer
    falsifiable teeth), and each session's private audit dir stays empty +
    byte-identical (MCP-owned-state isolation teeth);
  * the SAME no-write proof holds per session with
    ``AIWORKHUB_ALLOW_WRITES`` UNSET and =1, run concurrently in both
    children.

Checks (all must pass for ``concurrent_contract_v1`` true):

  C1  both concurrent sessions see every read-only tool via ``tools/list``;
  C2  both also see every write-gated tool (contract completeness);
  C3  every tool's inputSchema sha256 == the SAME frozen B108 fingerprint in
      BOTH concurrent sessions;
  C4  the two concurrent sessions agree on all fingerprints (no drift under
      concurrency);
  C5  concurrent-session temporal overlap confirmed (barrier released: both
      subprocesses alive at once);
  C6  no cross-session state bleed: each session's ``pending_for_runner`` echoes
      ONLY its own runner, and each private audit dir stays empty + isolated;
  C7  no queue/audit writes with ALLOW_WRITES UNSET, both sessions concurrent:
      each isolated audit dir byte-identical + empty, parent queue verify-intact;
  C8  same no-write proof with ALLOW_WRITES=1 in both children concurrently;
  C9  STDIO transport actually used out-of-process by both sessions;
  C10 ONLY the MCP server is launched by each session: python running
      ``-m aiworkhub.server`` -- no agent/model binary, no shell;
  C11 server.py holds no subprocess/exec/fork/shell launch code and
      ``launch_enabled()`` / ``LAUNCH_IMPLEMENTED`` stay False.

This harness NEVER enables writes for real, NEVER launches an agent/model,
makes NO network call, and logs NO secret values. It is isolation-safe: every
session owns a private mktemp audit dir, so the byte-identity / no-bleed proofs
cannot be perturbed by a concurrent worker.

Neural-control note: this is a deterministic transport/coordination VALIDATOR
(evidence extractor), not runtime cognition. Adapter/runner/topic SELECTION must
become a learned router (MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1); no
regex/keyword cue router is introduced here as launch authority.

Usage:
    PYTHONPATH=tools/geoai-task-mcp/src AIWORKHUB_REPO=/home/shrek/AIWorkHub \
    python3 tools/geoai-task-mcp/tests/mcp_stdio_concurrent_client_smoke.py \
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

TASK_ID = "CLAUDE_TASK_MCP_STDIO_CONCURRENT_CLIENT_SMOKE_B110_V1"

# Two DISTINCT concurrent-session identities. "alpha" is NOT a substring of
# "bravo" (and vice versa) so the no-bleed echo teeth is unambiguous.
RUNNER_A = "claude_task_mcp_concurrent_alpha_b110"
RUNNER_B = "claude_task_mcp_concurrent_bravo_b110"

# --- frozen read-only contract v1 (identical to B108/B109) ----------------
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

# The SAME byte-canonical sha256(inputSchema) values frozen at B108. A drift in
# ANY tool's input schema flips concurrent_contract_v1 to false over the pipe.
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
CONCURRENT_TIMEOUT = 180.0  # wall-clock guard for the two-session barrier round


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


def _readonly_args(runner: str, tag: str) -> dict[str, dict[str, Any]]:
    """Read-only tools_call payloads for a single concurrent session, keyed to
    that session's OWN runner identity so cross-session bleed is detectable."""
    return {
        "aiworkhub_task_health": {},
        "aiworkhub_task_review_queue": {},
        "aiworkhub_task_list": {"status": "pending", "limit": 3},
        "aiworkhub_task_show": {"task_id": TASK_ID},
        "aiworkhub_task_pending_for_runner": {"runner": runner},
        "aiworkhub_task_collision_guard": {"print_json": True},
        "aiworkhub_task_usage_report": {},
        "aiworkhub_task_audit_log_read": {"max_entries": 10},
        "aiworkhub_cli_adapter_plan_readonly": {
            "task_id": f"B110-PLAN-{tag}", "runner": runner, "topic": "task_mcp",
            "adapter_id": "claude_cli", "argv": ["claude", "-p", "hi"],
        },
        "aiworkhub_cli_adapter_audit_summary_readonly": {"max_entries": 10},
        "aiworkhub_cli_adapter_report_readonly": {
            "task_id": f"B110-REPORT-{tag}", "runner": runner, "topic": "task_mcp",
            "adapter_id": "claude_cli", "argv": ["codex", "exec", "review"],
        },
    }


def _server_params(audit_log: Path, allow_writes: bool) -> StdioServerParameters:
    """Build stdio params that launch ONLY the MCP server subprocess.

    command = this python interpreter, args = ``-m aiworkhub.server``.
    stdio_client spawns it directly over OS pipes (never a shell). Each session
    gets its OWN isolated audit-log path; ``AIWORKHUB_ALLOW_WRITES`` is
    set/unset per round to exercise both no-write branches.
    """
    env = get_default_environment()
    env["PYTHONPATH"] = SRC
    env["AIWORKHUB_REPO"] = str(core.repo_root())
    env["AIWORKHUB_AUDIT_LOG_PATH"] = str(audit_log)
    env.pop("AIWORKHUB_ALLOW_WRITES", None)
    if allow_writes:
        env["AIWORKHUB_ALLOW_WRITES"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "aiworkhub.server"],
        env=env,
        cwd=str(core.repo_root()),
    )


def _extract_structured(result: Any) -> dict[str, Any]:
    """Best-effort extraction of a tool's structured dict result."""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP wraps a bare return in {"result": ...} when it is not a dict;
        # our tools return dicts, so prefer the dict as-is.
        if set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    for c in getattr(result, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    return obj
            except (ValueError, TypeError):
                continue
    return {}


async def _drive_session(
    runner: str, tag: str, audit_log: Path, allow_writes: bool,
    barrier: asyncio.Barrier,
) -> dict[str, Any]:
    """One concurrent session: spawn server subprocess, initialize, list tools,
    barrier-sync so BOTH sessions are alive at once, then call every read-only
    tool. Returns this session's schemas + pending_for_runner echo + errors."""
    params = _server_params(audit_log, allow_writes=allow_writes)
    args_map = _readonly_args(runner, tag)
    out: dict[str, Any] = {
        "runner": runner, "schemas": {}, "pending_command": [],
        "any_tool_error": False, "error_tool": None, "initialized": False,
    }
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as s:
            await s.initialize()
            out["initialized"] = True
            listed = await s.list_tools()
            out["schemas"] = {t.name: t.inputSchema for t in listed.tools}
            # Force temporal overlap: neither session proceeds to tools/call
            # until BOTH have initialized + listed tools (both subprocs alive).
            await barrier.wait()
            for name in READONLY_TOOLS:
                r = await s.call_tool(
                    name, args_map[name], read_timeout_seconds=REQUEST_TIMEOUT
                )
                if r.isError:
                    out["any_tool_error"] = True
                    out["error_tool"] = name
                    break
                if name == "aiworkhub_task_pending_for_runner":
                    structured = _extract_structured(r)
                    cmd = structured.get("command", [])
                    out["pending_command"] = [str(x) for x in cmd] if isinstance(cmd, list) else []
            # Second barrier: both sessions finished their calls while both are
            # still alive (overlap spans the whole tools/call phase).
            await barrier.wait()
    return out


async def _concurrent_round(
    state_a: Path, state_b: Path, allow_writes: bool,
) -> dict[str, Any]:
    """Run BOTH sessions concurrently against their own isolated audit dirs and
    prove per-session no-write + no-bleed under real concurrency."""
    audit_a = state_a / "audit.jsonl"
    audit_b = state_b / "audit.jsonl"
    before_a = _snapshot_dir(state_a)
    before_b = _snapshot_dir(state_b)
    verify_before = core.run_taskctl(["verify"]).returncode

    barrier = asyncio.Barrier(2)
    res_a, res_b = await asyncio.wait_for(
        asyncio.gather(
            _drive_session(RUNNER_A, "A", audit_a, allow_writes, barrier),
            _drive_session(RUNNER_B, "B", audit_b, allow_writes, barrier),
        ),
        timeout=CONCURRENT_TIMEOUT,
    )

    after_a = _snapshot_dir(state_a)
    after_b = _snapshot_dir(state_b)
    verify_after = core.run_taskctl(["verify"]).returncode

    overlap_confirmed = bool(res_a["initialized"] and res_b["initialized"])
    no_tool_errors = not (res_a["any_tool_error"] or res_b["any_tool_error"])

    # no-bleed teeth: each session's pending_for_runner command echoes ONLY its
    # own runner identity, never the other session's.
    cmd_a = " ".join(res_a["pending_command"])
    cmd_b = " ".join(res_b["pending_command"])
    bleed_ok = bool(
        res_a["pending_command"] and res_b["pending_command"]
        and RUNNER_A in cmd_a and RUNNER_B not in cmd_a
        and RUNNER_B in cmd_b and RUNNER_A not in cmd_b
    )

    state_ok = (
        after_a == before_a and after_b == before_b
        and after_a == [] and after_b == []
    )
    return {
        "ok": bool(overlap_confirmed and no_tool_errors and bleed_ok and state_ok
                   and verify_before == 0 and verify_after == 0),
        "overlap_confirmed": overlap_confirmed,
        "no_tool_errors": no_tool_errors,
        "distinct_audit_dirs": str(state_a) != str(state_b),
        "session_a_sees_own_runner": RUNNER_A in cmd_a,
        "session_a_sees_other_runner": RUNNER_B in cmd_a,
        "session_b_sees_own_runner": RUNNER_B in cmd_b,
        "session_b_sees_other_runner": RUNNER_A in cmd_b,
        "no_cross_session_bleed": bleed_ok,
        "state_a_before": before_a, "state_a_after": after_a,
        "state_b_before": before_b, "state_b_after": after_b,
        "state_byte_identical": after_a == before_a and after_b == before_b,
        "state_empty": after_a == [] and after_b == [],
        "queue_verify_before_rc": verify_before,
        "queue_verify_after_rc": verify_after,
        "queue_verify_intact": verify_before == 0 and verify_after == 0,
        "schemas_a": res_a["schemas"],
        "schemas_b": res_b["schemas"],
    }


def run_smoke() -> dict[str, Any]:
    """Execute the full two-session concurrent stdio client smoke."""
    checks: dict[str, bool] = {}
    detail: dict[str, Any] = {}
    failing_check: str | None = None

    root = Path(tempfile.mkdtemp(prefix="aiworkhub_b110_concurrent_smoke_"))
    try:
        # --- C10 launched-command proof (both sessions) --------------------
        params_a = _server_params(root / "probe_a.jsonl", allow_writes=False)
        params_b = _server_params(root / "probe_b.jsonl", allow_writes=False)
        agent_hits: list[str] = []
        launch_ok = True
        for p in (params_a, params_b):
            cmd_lower = " ".join([p.command, *p.args]).lower()
            is_python = os.path.basename(p.command).lower().startswith("python")
            runs_server = p.args[:2] == ["-m", "aiworkhub.server"]
            scan = cmd_lower.replace("aiworkhub.server", "")
            hits = sorted({t for t in AGENT_MODEL_TOKENS if t in scan})
            agent_hits.extend(hits)
            launch_ok = launch_ok and is_python and runs_server and not hits
        checks["only_mcp_server_launched"] = bool(launch_ok)
        detail["server_command_normalized"] = ["<python>", "-m", "aiworkhub.server"]
        detail["launched_agent_or_model_token_hits"] = sorted(set(agent_hits))
        detail["concurrent_sessions"] = 2
        detail["session_runners"] = [RUNNER_A, RUNNER_B]
        detail["transport"] = (
            "two concurrent mcp.client.stdio.stdio_client + mcp.ClientSession "
            "sessions over real OS stdio pipes (out-of-process subprocesses, "
            "barrier-synced overlap)"
        )

        # --- Round 1: ALLOW_WRITES unset, both sessions concurrent ---------
        sa1, sb1 = root / "unset_a", root / "unset_b"
        sa1.mkdir(); sb1.mkdir()
        r_unset = asyncio.run(_concurrent_round(sa1, sb1, allow_writes=False))

        # --- Round 2: ALLOW_WRITES=1 in both children, concurrent ----------
        sa2, sb2 = root / "set_a", root / "set_b"
        sa2.mkdir(); sb2.mkdir()
        r_set = asyncio.run(_concurrent_round(sa2, sb2, allow_writes=True))

        # --- C1/C2 visibility (both concurrent sessions) -------------------
        schemas_a = r_unset["schemas_a"]
        schemas_b = r_unset["schemas_b"]
        vis_a, vis_b = set(schemas_a), set(schemas_b)
        checks["stdio_transport_used"] = bool(schemas_a) and bool(schemas_b)
        checks["readonly_tools_visible"] = (
            set(READONLY_TOOLS) <= vis_a and set(READONLY_TOOLS) <= vis_b
        )
        checks["write_gated_tools_visible"] = (
            set(WRITE_GATED_TOOLS) <= vis_a and set(WRITE_GATED_TOOLS) <= vis_b
        )
        detail["readonly_tools_visible"] = sorted(n for n in READONLY_TOOLS if n in vis_a)
        detail["write_gated_tools_visible"] = sorted(n for n in WRITE_GATED_TOOLS if n in vis_a)
        detail["total_tools_visible"] = len(vis_a)
        detail["total_tools_visible_session_b"] = len(vis_b)

        # --- C3/C4 frozen fingerprints in BOTH sessions + cross agreement --
        fp_a = {n: _canon_fp(s) for n, s in schemas_a.items()}
        fp_b = {n: _canon_fp(s) for n, s in schemas_b.items()}
        mism_a = sorted(n for n in FROZEN_SCHEMA_FINGERPRINTS
                        if fp_a.get(n) != FROZEN_SCHEMA_FINGERPRINTS[n])
        mism_b = sorted(n for n in FROZEN_SCHEMA_FINGERPRINTS
                        if fp_b.get(n) != FROZEN_SCHEMA_FINGERPRINTS[n])
        checks["schema_fingerprints_match_frozen"] = not mism_a and not mism_b
        checks["schema_deterministic_across_concurrent_sessions"] = fp_a == fp_b
        detail["schema_fingerprint_mismatches_session_a"] = mism_a
        detail["schema_fingerprint_mismatches_session_b"] = mism_b
        detail["frozen_schema_fingerprints"] = FROZEN_SCHEMA_FINGERPRINTS

        # --- C5 overlap, C6 no-bleed, C7/C8 no-write per round -------------
        checks["concurrent_sessions_overlap"] = bool(
            r_unset["overlap_confirmed"] and r_set["overlap_confirmed"]
        )
        checks["no_cross_session_state_bleed"] = bool(
            r_unset["no_cross_session_bleed"] and r_set["no_cross_session_bleed"]
            and r_unset["distinct_audit_dirs"] and r_set["distinct_audit_dirs"]
        )
        checks["no_write_allow_unset"] = bool(
            r_unset["ok"] and r_unset["state_byte_identical"]
            and r_unset["state_empty"] and r_unset["queue_verify_intact"]
        )
        checks["no_write_allow_set"] = bool(
            r_set["ok"] and r_set["state_byte_identical"]
            and r_set["state_empty"] and r_set["queue_verify_intact"]
        )
        # Compact, path-free round snapshots for the eval artifact.
        detail["round_allow_unset"] = {
            k: r_unset[k] for k in (
                "overlap_confirmed", "no_tool_errors", "distinct_audit_dirs",
                "session_a_sees_own_runner", "session_a_sees_other_runner",
                "session_b_sees_own_runner", "session_b_sees_other_runner",
                "no_cross_session_bleed", "state_byte_identical", "state_empty",
                "queue_verify_before_rc", "queue_verify_after_rc",
                "queue_verify_intact", "ok",
            )
        }
        detail["round_allow_set"] = {
            k: r_set[k] for k in detail["round_allow_unset"]
        }

        # --- C11 server holds no launch code -------------------------------
        server_src = (Path(SRC) / "aiworkhub" / "server.py").read_text(encoding="utf-8")
        launch_hits = [p for p in LAUNCH_PATTERNS if p in server_src]
        checks["server_no_launch_code"] = (
            not launch_hits
            and ro.launch_enabled() is False
            and ro.LAUNCH_IMPLEMENTED is False
        )
        detail["server_launch_pattern_hits"] = launch_hits
        detail["launch_enabled"] = ro.launch_enabled()
        detail["launch_implemented"] = ro.LAUNCH_IMPLEMENTED
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for name, passed in checks.items():
        if not passed:
            failing_check = name
            break

    frozen = failing_check is None

    # Path-independent signature so the shell can assert determinism across two
    # independent process runs (no tmp paths / pids leak into it).
    run_signature = {
        "checks": checks,
        "readonly_tools_visible": detail["readonly_tools_visible"],
        "write_gated_tools_visible": detail["write_gated_tools_visible"],
        "total_tools_visible": detail["total_tools_visible"],
        "total_tools_visible_session_b": detail["total_tools_visible_session_b"],
        "frozen_schema_fingerprints": FROZEN_SCHEMA_FINGERPRINTS,
        "schema_fingerprint_mismatches_session_a": detail["schema_fingerprint_mismatches_session_a"],
        "schema_fingerprint_mismatches_session_b": detail["schema_fingerprint_mismatches_session_b"],
        "server_command_normalized": detail["server_command_normalized"],
        "launched_agent_or_model_token_hits": detail["launched_agent_or_model_token_hits"],
        "server_launch_pattern_hits": detail["server_launch_pattern_hits"],
        "concurrent_sessions": detail["concurrent_sessions"],
        "session_runners": detail["session_runners"],
        "round_allow_unset": detail["round_allow_unset"],
        "round_allow_set": detail["round_allow_set"],
        "failing_check": failing_check,
        "concurrent_contract_v1": frozen,
    }
    detail["run_signature"] = run_signature

    return {
        "eval_id": "mcp_stdio_concurrent_client_smoke_b110_v1",
        "task_id": TASK_ID,
        "mode": "mcp_stdio_concurrent_readonly_no_agent_launch",
        "client_path": (
            "two concurrent mcp.ClientSession over mcp.client.stdio.stdio_client "
            "(real out-of-process OS pipes; barrier-synced overlap; "
            "initialize/tools_list/tools_call)"
        ),
        "extends": "mcp_stdio_subprocess_client_smoke_b109_v1",
        "concurrent_contract_v1": frozen,
        "failing_check": failing_check,
        "readonly_tool_count": len(READONLY_TOOLS),
        "write_gated_tool_count": len(WRITE_GATED_TOOLS),
        "concurrent_sessions": 2,
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
    return 0 if result["concurrent_contract_v1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
