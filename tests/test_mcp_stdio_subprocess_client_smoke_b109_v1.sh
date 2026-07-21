#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_stdio_subprocess_client_smoke_b109_v1.sh
# Drives the aiworkhub server as its OWN out-of-process subprocess over
# REAL OS stdio pipes (mcp.client.stdio.stdio_client + mcp.ClientSession) and
# asserts the frozen tool contract v1 STILL holds across the pipe: the exact
# 33-tool inventory is visible over tools/list, every input schema is
# byte-stable vs the B109 frozen fingerprints, and NO queue/audit writes with
# AIWORKHUB_ALLOW_WRITES unset AND =1. Also asserts ONLY the MCP server
# process is launched (no agent/model, no shell=True), its launch gate is
# forced closed, and server.py holds no direct spawn primitive. Parent task
# queue must stay verify-intact.
#
# Isolation: the smoke owns a private mktemp audit state dir (passed to the
# child via env) and the result JSON is a per-run mktemp path. Parallel-safe;
# no shared file is mutated.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
SMOKE="$MCPROOT/tests/mcp_stdio_client_smoke.py"
SERVER_SRC="$MCPROOT/src/aiworkhub/server.py"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_b109_stdio_smoke.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
unset AIWORKHUB_ALLOW_WRITES AIWORKHUB_ALLOW_LAUNCH

echo "=== MCP STDIO Subprocess Client Smoke Test B109 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "TMP=$TMPDIR_STATE"

# --- 0. server.py holds no direct spawn primitive (defense in depth) -------
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$SERVER_SRC"; then
        echo "FAIL: forbidden launch pattern '$pat' found in server.py"
        exit 1
    fi
done
echo "server.py: no direct launch/exec/shell primitive: OK"

# --- 0b. smoke drives a REAL stdio transport and never uses a shell --------
for req in "StdioServerParameters" "stdio_client" "ClientSession"; do
    if ! grep -Fq -- "$req" "$SMOKE"; then
        echo "FAIL: smoke missing real stdio transport marker '$req'"
        exit 1
    fi
done
if grep -Fq -- "shell=True" "$SMOKE"; then
    echo "FAIL: smoke uses shell=True (forbidden)"
    exit 1
fi
echo "smoke: real stdio transport, no shell=True: OK"

# --- 1. run the real out-of-process stdio smoke; exit 0 = frozen_contract --
RESULT="$TMPDIR_STATE/result.json"
python3 "$SMOKE" --out "$RESULT" >/dev/null 2>"$TMPDIR_STATE/smoke.stderr"
echo "smoke exit 0 (frozen_contract_v1=true): OK"

# --- 2. assert every gate in the emitted result ---------------------------
python3 - "$RESULT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))

assert d["frozen_contract_v1"] is True, ("frozen_contract_v1 not true", d.get("failing_check"))
assert d["failing_check"] is None, d["failing_check"]
assert d["mode"] == "mcp_stdio_client_smoke_no_agent_launch", d["mode"]
assert d["extends"] == "mcp_client_smoke_contract_freeze_b108_v1", d["extends"]

checks = d["checks"]
expected = {
    "readonly_tools_visible",
    "write_gated_tools_visible",
    "schema_fingerprints_match_frozen",
    "schema_deterministic_across_sessions",
    "no_write_allow_unset",
    "no_write_allow_set",
    "stdio_transport_used",
    "only_mcp_server_launched",
    "server_no_launch_code",
    "tool_inventory_matches_frozen",
    "launch_gate_forced_closed",
    "no_launch_side_effects",
}
assert expected <= set(checks), sorted(expected - set(checks))
for name in expected:
    assert checks[name] is True, f"check failed: {name}"

# B108 compatibility subsets remain intact; B109 freezes all 33 visible tools.
assert d["readonly_tool_count"] == 11, d["readonly_tool_count"]
assert d["write_gated_tool_count"] == 4, d["write_gated_tool_count"]
assert len(d["detail"]["readonly_tools_visible"]) == 11, d["detail"]["readonly_tools_visible"]

expected_tools = {
    "aiworkhub_agent_cancel_task",
    "aiworkhub_agent_collect_result",
    "aiworkhub_agent_launch_task",
    "aiworkhub_agent_list_processes",
    "aiworkhub_agent_task_status",
    "aiworkhub_cli_adapter_audit_summary_readonly",
    "aiworkhub_cli_adapter_plan_readonly",
    "aiworkhub_cli_adapter_report_readonly",
    "aiworkhub_completion_inbox",
    "aiworkhub_launch_queue_audit_summary_readonly",
    "aiworkhub_launch_queue_describe_readonly",
    "aiworkhub_launch_queue_evaluate_readonly",
    "aiworkhub_supervisor_loop_status",
    "aiworkhub_task_audit_log_read",
    "aiworkhub_task_auto_pickup",
    "aiworkhub_task_auto_pickup_dryrun",
    "aiworkhub_task_codex_handoff",
    "aiworkhub_task_codex_handoff_markdown",
    "aiworkhub_task_collision_guard",
    "aiworkhub_task_cost_ledger",
    "aiworkhub_task_export_jsonl",
    "aiworkhub_task_health",
    "aiworkhub_task_list",
    "aiworkhub_task_mark_done",
    "aiworkhub_task_mark_review",
    "aiworkhub_task_pending_for_runner",
    "aiworkhub_task_queue_request",
    "aiworkhub_task_reject_review",
    "aiworkhub_task_review_queue",
    "aiworkhub_task_review_summarize",
    "aiworkhub_task_show",
    "aiworkhub_task_stale_recovery_recommend",
    "aiworkhub_task_usage_report",
}
assert len(expected_tools) == 33
assert d["detail"]["total_tools_visible"] == 33, d["detail"]["total_tools_visible"]
assert d["detail"]["tools_visible"] == sorted(expected_tools), d["detail"]["tools_visible"]
assert d["detail"]["frozen_tool_names"] == sorted(expected_tools)
assert d["detail"]["missing_tools"] == [], d["detail"]["missing_tools"]
assert d["detail"]["unexpected_tools"] == [], d["detail"]["unexpected_tools"]

# Exact canonical input-schema fingerprints for every visible tool.
assert d["detail"]["schema_fingerprint_mismatches"] == [], d["detail"]["schema_fingerprint_mismatches"]
frozen_fp = d["detail"]["frozen_schema_fingerprints"]
current_fp = d["detail"]["current_schema_fingerprints"]
assert set(frozen_fp) == expected_tools, sorted(set(frozen_fp) ^ expected_tools)
assert current_fp == frozen_fp, "live input schemas differ from the B109 freeze"

# The equality gates must reject both schema mutation and inventory expansion.
mutated_fp = dict(frozen_fp)
mutated_fp["aiworkhub_agent_launch_task"] = "deadbeef"
assert current_fp != mutated_fp
expanded_tools = set(expected_tools)
expanded_tools.add("aiworkhub_unexpected_tool")
assert set(current_fp) != expanded_tools

# no-write proof: byte-identical AND empty state dir in BOTH rounds
for rk in ("round_allow_unset", "round_allow_set"):
    r = d["detail"][rk]
    assert r["state_byte_identical"] is True, (rk, "state not byte-identical")
    assert r["state_empty"] is True, (rk, "state dir not empty")
    assert r["queue_verify_intact"] is True, (rk, "queue verify not intact")
    assert r["state_after"] == r["state_before"] == [], (rk, r)

# only-the-server launched: normalized python + server module, no agent tokens
assert d["detail"]["server_command_normalized"] == ["<python>", "-m", "aiworkhub.server"], \
    d["detail"]["server_command_normalized"]
assert d["detail"]["launched_agent_or_model_token_hits"] == [], \
    d["detail"]["launched_agent_or_model_token_hits"]

# Launch capability exists, but this smoke forces authority closed and leaves
# the isolated audit/process state empty in both rounds.
assert d["detail"]["server_launch_pattern_hits"] == [], d["detail"]["server_launch_pattern_hits"]
assert d["detail"]["child_launch_gate"] == "0"
assert d["detail"]["launch_enabled"] is False
assert d["detail"]["launch_implemented"] is True
assert d["detail"]["legacy_readonly_launch_enabled"] is False
assert d["detail"]["legacy_readonly_launch_implemented"] is False

# authority flags all stay false (incl. agent launch authority)
for k, v in d["authority_flags"].items():
    assert v is False, f"authority flag {k} not false"

print("PASS: all stdio-subprocess client-smoke contract-freeze gates satisfied")
print("  readonly tools visible :", len(d["detail"]["readonly_tools_visible"]))
print("  total tools frozen     :", d["detail"]["total_tools_visible"])
print("  transport              :", d["detail"]["transport"])
print("  frozen_contract_v1     :", d["frozen_contract_v1"])
PYEOF

# --- 3. parent task queue intact ------------------------------------------
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
