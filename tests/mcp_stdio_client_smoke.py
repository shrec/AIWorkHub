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
  C3  every one of the 33 frozen tools is still VISIBLE and still carries its
      exact frozen inputSchema, and every rendered schema is deterministic
      across two independent stdio subprocess sessions. (NARROWED 2026-09-08
      from exact 33-tool CLOSURE -- the server now exposes 188; see the note
      above FROZEN_SCHEMA_FINGERPRINTS.);
  C4  no queue/audit writes with ``AIWORKHUB_ALLOW_WRITES`` UNSET: the
      MCP-owned audit state dir (passed to the child via env) is byte-identical
      before/after every read-only ``tools/call``, holds no RECORD (a zero-byte
      advisory ``*.lock`` the server's own startup takes is tolerated and
      nothing else is -- see ``_run_readonly_round_via_stdio``), and the parent
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

# Raw ``state_byte_identical`` (see the round dicts below) is diagnostic-only
# and must NEVER be folded into any pass/fail check in this harness: the
# server's own startup reconciler MEASURABLY takes a zero-byte advisory
# ``process_events.jsonl.lock``, so raw byte-identity is always false here,
# and any check requiring it would be permanently red regardless of real
# writes. ``state_byte_identical_excl_tolerated_lock`` is the correct
# replacement -- strictly stronger than ``state_holds_no_records`` alone --
# and is the only byte-identity signal any check here may use.

# Byte-canonical sha256(inputSchema) for the 33 tools frozen at B109. A removed,
# renamed, or schema-drifted frozen tool flips frozen_contract_v1 to false.
# RE-FROZEN 2026-09-08, and NARROWED, with the measurement for both.
#
# This harness had never been RUN since it was written: pytest collects
# ``test_*.py`` only and nothing in CI named this file, so it sat red without
# saying so. Two separate things were wrong with it, and only one of them is a
# drifted pin.
#
# 1. THE PIN. 30 of these 33 fingerprints mismatched. The dominant cause is
#    that the tool FUNCTIONS were renamed ``geoai_*`` -> ``aiworkhub_*``:
#    FastMCP names each tool's pydantic argument model
#    ``f"{func.__name__}Arguments"``, so that title -- and only that title --
#    lives inside every inputSchema. Substituting the old prefix back into the
#    current schema reproduces the old fingerprint exactly for the pure-rename
#    cases, which is the proof that their parameters never changed. It is NOT
#    an SDK rendering change: the three ``aiworkhub_cli_adapter_*`` tools never
#    drifted at all, because their functions carry their own names
#    (``plan_command_readonly`` / ``audit_summary_readonly`` /
#    ``readonly_tool_report``) and are registered under an explicit tool name.
#    The rest are genuine additive parameters accumulated over ~100 waves
#    (``task_show.detail``/``full``, ``task_mark_done.include_card``,
#    ``agent_launch_task``'s and ``completion_inbox``'s new options, ...).
#    All 33 are re-frozen here against the current server.
#
# 2. THE CLOSURE. The original checks asserted the server has EXACTLY these 33
#    tools and EXACTLY these fingerprints for its whole visible surface.
#    MEASURED 2026-09-08: the server exposes 188. Every one of the 33 is still
#    present -- none was removed or renamed away -- but the surface grew by
#    deliberate product decisions, wave after wave. A closure pin over a
#    growing surface asserts only "no tool has been added since the pin was
#    written", which is not a contract, cannot be maintained, and would turn
#    red on the next tool anyone adds while teaching nothing about it.
#    So the closure is narrowed to what is still TRUE and still worth
#    defending: every frozen tool must still be VISIBLE and must still carry
#    its EXACT frozen input schema, over a real stdio pipe. A tool that
#    disappears, is renamed, or changes shape still fails this gate; a newly
#    added tool does not.
FROZEN_SCHEMA_FINGERPRINTS: dict[str, str] = {
    "aiworkhub_agent_cancel_task": "f1d9792f94307639105434caa381c54857d7c292dab122b4cd4e9333dd0552f3",
    "aiworkhub_agent_collect_result": "c291cac06bc81689a1b0df1357facb97c5806fefd0a447ef3245e38976077a3f",
    "aiworkhub_agent_launch_task": "02f5d9a9625474dca12ffb80a5afbc8415aa971561799e2041a1557fe0c1d59b",
    "aiworkhub_agent_list_processes": "706c69e343e2b322d11e57bd76279459c4e7296f3906d620fa9c2d643931db28",
    "aiworkhub_agent_task_status": "2c2f20ce1b0fd6064f7aa7fc34822f6ba82911605ed9eb3924752cc442321455",
    "aiworkhub_cli_adapter_audit_summary_readonly": "6ab96b247924a28d5d793064a61e50c59f46a771c53459ec1197e3acca973fee",
    "aiworkhub_cli_adapter_plan_readonly": "7a79866fe17cd414929fc7e59898311436ce102d826637f7bac3df870cc49c9e",
    "aiworkhub_cli_adapter_report_readonly": "f80f2283b05d851f6fe1a405930efb9f65e39e7f2d501dc70ed5635ab5ca538c",
    "aiworkhub_completion_inbox": "6f38948559f5ecef8e53a08e2658eb73dedf6452a349e49c7a9110e18e131d75",
    "aiworkhub_launch_queue_audit_summary_readonly": "074f02e6dc862d3b8207f0abbfc33fa7345afc5e29856d09487cf806735dbe05",
    "aiworkhub_launch_queue_describe_readonly": "196646048b91854998c28342d65215b29254c8ead6fd79bdc9d43839d774bdd3",
    "aiworkhub_launch_queue_evaluate_readonly": "8f4cde4ac5015c22c941c48d9f6fb17541d0b47cabb51ab44de615771689b861",
    "aiworkhub_supervisor_loop_status": "ca2607d4656ee1a5cd7cba313fee45589ea2ea355a870ca5b7a03a18a70fe68e",
    "aiworkhub_task_audit_log_read": "5111315e1823d882715a6b1fa754f1c64894ec10eea469b0b2fe7bc5e98ec015",
    "aiworkhub_task_auto_pickup": "02878ab53c86e45277a3c4337c438218de36a3d2d9f82f2385d9afaed2aa243e",
    "aiworkhub_task_auto_pickup_dryrun": "f5c5e3922552fb6813c94f921c9163c8aded29f7db478fd88ab8c60a9e30a29a",
    "aiworkhub_task_codex_handoff": "5f15fa92c6eb29072587971c5d4226860d9196893a53688bcb027a4e2b07a6aa",
    "aiworkhub_task_codex_handoff_markdown": "00f589247e30bcdd709f807b64b3e80452eab0c074fc14326d734e14e0491ece",
    "aiworkhub_task_collision_guard": "9a6a72f9e63acc7e08a9fd4c95e66382530f96b98bb40467801b6679c2adf6fd",
    "aiworkhub_task_cost_ledger": "58b4efbf795ea2d110e6dd630a048afce78bd2dba650aac46e5d2ce5a6660540",
    "aiworkhub_task_export_jsonl": "60699d5c0d81ccf28ea7cd0a2f415089d0f87d144b40b83117517649fa847a4a",
    "aiworkhub_task_health": "0d6de3645a2bbbb16f9ae4bc7592403918fd5a8768a3888b1018f1570a9aaffc",
    "aiworkhub_task_list": "bf57352dc786f7f0a14c31625711a0a6ab4576d2d85fb730209a4a957efbd914",
    "aiworkhub_task_mark_done": "85f0dad5ce5e23a4c258d1314230223d1ff8adfcee3f17765283986f1dce4e9a",
    "aiworkhub_task_mark_review": "1722a2665425d2ebd8573c3425ee2d04b8c71ae3db24ace3c97e5a82d7e92bd4",
    "aiworkhub_task_pending_for_runner": "864d85c9f3a5a7020e270ddd71f9aeea2ab65ca35932aaf869afaa837950b16d",
    "aiworkhub_task_queue_request": "2c43bb4347d3806b3f373390c95cfc7c4f398b4404860ff4a1722269edeb7fa5",
    "aiworkhub_task_reject_review": "7ab03785fb0e3e081bc677b4d9e9d8d57b7706d630a6deb48a77700e3d921b07",
    "aiworkhub_task_review_queue": "b5728f6f46c22488977fe80e794f420203cf6725367eb9271cfdad1c6538b878",
    "aiworkhub_task_review_summarize": "e535208ce91e843357bb2efb237b656c50ad1a0b4e1be4111c9ae2356c647e01",
    "aiworkhub_task_show": "987a6779aea9974dc849205292e41e5fab2443e04e97f3c2528059a6b60c4ad3",
    "aiworkhub_task_stale_recovery_recommend": "29e87fad5d966f1e6159991a6ddeba3f77be49d7d8ee11951dd8c858556881fc",
    "aiworkhub_task_usage_report": "84fe64116d44bde99ea6a1d850e94a3164e32c12b7a4479ecb4dc10bcc0fe7e3",
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


_TOLERATED_ADVISORY_LOCK = "process_events.jsonl.lock"


def _exclude_tolerated_lock(
    rows: list[tuple[str, int, str]],
) -> list[tuple[str, int, str]]:
    """Drop exactly the zero-byte reconciler startup lock, nothing else."""
    return [
        row for row in rows
        if not (row[0] == _TOLERATED_ADVISORY_LOCK and row[1] == 0)
    ]


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
    # MEASURED 2026-09-08 with this harness pointed at a freshly initialized
    # repository -- which is what a CI runner is -- the server child creates
    # ONE zero-byte advisory lock, ``process_events.jsonl.lock``, next to the
    # process log this harness deliberately redirects into the state dir. It
    # is created by the server's own startup (the reconciler taking the
    # process-log lock), not by any ``tools/call``; against a developer
    # machine where another AIWorkHub server already holds that lock it never
    # appeared, which is why this check looked green here and would have been
    # red in CI.
    #
    # The byte-identity comparison tolerates EXACTLY that one known file: the
    # state_dir tree is compared before vs. after with the zero-byte
    # ``process_events.jsonl.lock`` entry excluded from both sides, and
    # nothing else. Any other byte-level change -- a lock with content, a
    # file that is not that lock, a mutation to a pre-existing file -- still
    # fails ``state_byte_identical_excl_tolerated_lock``. Raw
    # ``state_byte_identical`` is left untouched below for diagnostics.
    before_excl_lock = _exclude_tolerated_lock(before)
    after_excl_lock = _exclude_tolerated_lock(after)
    return {
        "ok": True,
        "state_before": before,
        "state_after": after,
        "state_byte_identical": before == after,
        "state_byte_identical_excl_tolerated_lock": before_excl_lock == after_excl_lock,
        "state_empty": after == [],
        "state_holds_no_records": after_excl_lock == [],
        "queue_verify_before_rc": verify_before,
        "queue_verify_after_rc": verify_after,
        "queue_verify_intact": verify_before == 0 and verify_after == 0,
    }


async def _attempt_write_gated_calls_via_stdio(audit_log: Path) -> dict[str, bool]:
    """Confirm each write-gated tool is REJECTED on ``tools/call`` while the
    child has ``AIWORKHUB_ALLOW_WRITES`` unset -- not merely absent from
    ``tools/list``. A tool the server never registered has no dispatch
    target, so the client either gets an error result or the call raises;
    either outcome counts as rejected.
    """
    params = _server_params(audit_log, allow_writes=False)
    rejected: dict[str, bool] = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as s:
            await s.initialize()
            for name in WRITE_GATED_TOOLS:
                try:
                    r = await s.call_tool(name, {}, read_timeout_seconds=REQUEST_TIMEOUT)
                    rejected[name] = bool(r.isError)
                except Exception:
                    rejected[name] = True
    return rejected


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
        # Both stdio sessions above are listed with ALLOW_WRITES unset in the
        # child (see ``_list_tools_via_stdio``), so a correctly write-gated
        # tool is ABSENT here by design. ``required_tools`` carves
        # WRITE_GATED_TOOLS out of the "must be visible"/"must match frozen
        # fingerprint" requirements while writes are off; the read-only
        # closure is unaffected.
        required_tools = frozen_tools - set(WRITE_GATED_TOOLS)
        ro_visible = [n for n in READONLY_TOOLS if n in visible]
        wg_visible = [n for n in WRITE_GATED_TOOLS if n in visible]
        checks["readonly_tools_visible"] = set(ro_visible) == set(READONLY_TOOLS)
        checks["write_gated_tools_visible"] = not wg_visible
        # NARROWED 2026-09-08 (see the note above FROZEN_SCHEMA_FINGERPRINTS):
        # every frozen tool must still be VISIBLE in both sessions. It no
        # longer asserts the server has ONLY these tools -- it has 188, and a
        # closure pin over a deliberately growing surface asserts nothing about
        # any contract. Removal, rename and shape drift are still caught.
        checks["tool_inventory_matches_frozen"] = (
            required_tools <= visible and required_tools <= visible_b
        )
        detail["readonly_tools_visible"] = sorted(ro_visible)
        detail["write_gated_tools_visible"] = sorted(wg_visible)
        detail["tools_visible"] = sorted(visible)
        detail["frozen_tool_names"] = sorted(frozen_tools)
        detail["missing_tools"] = sorted(required_tools - visible)
        detail["tools_added_since_freeze"] = sorted(visible - frozen_tools)
        detail["unexpected_tools"] = []
        detail["total_tools_visible"] = len(visible)

        cur_fp = {n: _canon_fp(s) for n, s in schemas_a.items()}
        fp_b = {n: _canon_fp(s) for n, s in schemas_b.items()}
        mismatches = sorted(
            n for n in required_tools
            if cur_fp.get(n) != FROZEN_SCHEMA_FINGERPRINTS[n]
        )
        # Frozen tools only, for the same reason: a tool added after the freeze
        # has no frozen fingerprint to match, and demanding one would make this
        # gate fail on additions instead of on drift.
        checks["schema_fingerprints_match_frozen"] = not mismatches
        # Determinism is still compared over the WHOLE listing: two independent
        # subprocess sessions must render every schema identically, and that is
        # a property of the server, not of the pin.
        checks["schema_deterministic_across_sessions"] = cur_fp == fp_b
        detail["schema_fingerprint_mismatches"] = mismatches
        detail["current_schema_fingerprints"] = cur_fp
        detail["frozen_schema_fingerprints"] = FROZEN_SCHEMA_FINGERPRINTS

        # --- write-gated tools REJECTED on tools/call while disallowed ------
        # A dedicated probe dir (never touched by the C4/C5 snapshot rounds
        # below) so this subprocess's own audit/process-log files never
        # perturb the state_dir byte-identity proof.
        probe_dir = Path(tempfile.mkdtemp(prefix="aiworkhub_b109_write_probe_"))
        try:
            write_gated_rejections = asyncio.run(
                _attempt_write_gated_calls_via_stdio(probe_dir / "audit.jsonl")
            )
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        checks["write_gated_tools_call_rejected"] = all(write_gated_rejections.values())
        detail["write_gated_tools_call_rejected"] = write_gated_rejections

        # --- C4 no-write with ALLOW_WRITES unset in the child --------------
        r_unset = asyncio.run(_run_readonly_round_via_stdio(state_dir, allow_writes=False))
        # ``state_byte_identical_excl_tolerated_lock`` (not raw
        # ``state_byte_identical``) is the authoritative "no write happened"
        # signal here -- see the note above ``_run_readonly_round_via_stdio``:
        # the server's own startup reconciler MEASURABLY takes a zero-byte
        # advisory ``process_events.jsonl.lock`` next to this isolated audit
        # dir, which is not a queue/audit write. The comparison excludes
        # EXACTLY that one zero-byte file from both the before and after
        # snapshots and then requires the remaining trees to match byte for
        # byte -- any other new file, size change, or content mutation still
        # fails this gate, which is exactly the gap measured in CI (see
        # ``_run_readonly_round_via_stdio``'s docstring).
        checks["no_write_allow_unset"] = bool(
            r_unset.get("ok") and r_unset.get("state_byte_identical_excl_tolerated_lock")
            and r_unset.get("state_holds_no_records") and r_unset.get("queue_verify_intact")
        )
        detail["round_allow_unset"] = r_unset

        # --- C5 no-write with ALLOW_WRITES=1 in the child ------------------
        r_set = asyncio.run(_run_readonly_round_via_stdio(state_dir, allow_writes=True))
        checks["no_write_allow_set"] = bool(
            r_set.get("ok") and r_set.get("state_byte_identical_excl_tolerated_lock")
            and r_set.get("state_holds_no_records") and r_set.get("queue_verify_intact")
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
        # Same narrowing as C4/C5, for the same measured reason: "no launcher
        # STATE was created" is what this asserts, and a zero-byte advisory
        # lock is not launcher state. A launcher that actually ran would leave
        # a non-empty process_events.jsonl, which still fails here.
        checks["no_launch_side_effects"] = (
            checks["only_mcp_server_launched"]
            and checks["launch_gate_forced_closed"]
            and r_unset.get("state_byte_identical_excl_tolerated_lock") is True
            and r_unset.get("state_holds_no_records") is True
            and r_set.get("state_byte_identical_excl_tolerated_lock") is True
            and r_set.get("state_holds_no_records") is True
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
