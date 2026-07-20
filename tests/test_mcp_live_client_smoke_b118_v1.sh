#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_live_client_smoke_b118_v1.sh
# Live MCP client smoke test: exercises the geoai-task-mcp server over real
# OS stdio pipes from the local development environment and records whether
# the server is usable.  Reuses the B109 stdio subprocess smoke as the stdio
# transport driver, then adds B118-specific live-client checks:
#   - GEOAI_TASK_MCP_ALLOW_WRITES=0 enforced
#   - health + all 11 read-only tools exercised over stdio
#   - parent queue unchanged before/after
#   - taskctl verify passes
#
# Isolation: owns a private mktemp dir for audit state and result output.
# Parallel-safe; no shared file is mutated.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
SMOKE="$MCPROOT/tests/mcp_stdio_client_smoke.py"
SERVER_SRC="$MCPROOT/src/geoai_task_mcp/server.py"
EVAL_OUT="$MCPROOT/eval/mcp_live_client_smoke_b118_v1.json"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/geoai_b118_live_client_smoke.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== MCP Live Client Smoke Test B118 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo "TMP=$TMPDIR_STATE"

# --- 0. prerequisite: server.py holds no process-launch code ---------------
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$SERVER_SRC"; then
        echo "FAIL: forbidden launch pattern '$pat' found in server.py"
        exit 1
    fi
done
echo "[0] server.py: no launch/exec/shell code: OK"

# --- 0b. smoke driver uses real stdio transport ---------------------------
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
echo "[1] smoke: real stdio transport, no shell=True: OK"

# --- 1. snapshot parent queue BEFORE --------------------------------------
echo ""
echo "=== Queue snapshot BEFORE ==="
python3 "$ROOT/AITools/taskctl.py" verify > "$TMPDIR_STATE/verify_before.log" 2>&1 || {
    echo "FAIL: taskctl verify BEFORE failed"
    cat "$TMPDIR_STATE/verify_before.log"
    exit 1
}
echo "[2] queue verify BEFORE: PASS"

# --- 2. E2E LIVE CLIENT: exercise health + all 11 read-only tools ---------
echo ""
echo "=== Live Client Tool Exercise (read-only) ==="
RESULT="$TMPDIR_STATE/result.json"
python3 "$SMOKE" --out "$RESULT" >/dev/null 2>"$TMPDIR_STATE/smoke.stderr" || {
    echo "FAIL: stdio client smoke exited non-zero"
    cat "$TMPDIR_STATE/smoke.stderr"
    exit 1
}
echo "[3] stdio client smoke exit 0 (frozen_contract_v1=true): OK"

# --- 3. assert every smoke gate -------------------------------------------
python3 - "$RESULT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))

assert d["frozen_contract_v1"] is True, ("frozen_contract_v1 not true", d.get("failing_check"))
assert d["failing_check"] is None, d["failing_check"]
assert d["mode"] == "mcp_stdio_client_smoke_no_agent_launch", d["mode"]

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
}
assert expected <= set(checks), sorted(expected - set(checks))
for name in expected:
    assert checks[name] is True, f"check failed: {name}"

assert d["readonly_tool_count"] >= 11, f"readonly_tool_count={d['readonly_tool_count']} < 11"
assert d["write_gated_tool_count"] == 4, f"write_gated_tool_count={d['write_gated_tool_count']}"
assert len(d["detail"]["readonly_tools_visible"]) >= 11, f"visible readonly={len(d['detail']['readonly_tools_visible'])} < 11"
assert d["detail"]["total_tools_visible"] >= 15, f"total_tools_visible={d['detail']['total_tools_visible']} < 15"
assert d["detail"]["schema_fingerprint_mismatches"] == [], f"fingerprint mismatches: {d['detail']['schema_fingerprint_mismatches']}"

for rk in ("round_allow_unset", "round_allow_set"):
    r = d["detail"][rk]
    assert r["state_byte_identical"] is True, (rk, "state not byte-identical")
    assert r["state_empty"] is True, (rk, "state dir not empty")
    assert r["queue_verify_intact"] is True, (rk, "queue verify not intact")

assert d["detail"]["server_command_normalized"] == ["<python>", "-m", "geoai_task_mcp.server"]
assert d["detail"]["launched_agent_or_model_token_hits"] == []
assert d["detail"]["server_launch_pattern_hits"] == []
assert d["detail"]["launch_enabled"] is False
assert d["detail"]["launch_implemented"] is False

for k, v in d["authority_flags"].items():
    assert v is False, f"authority flag {k} not false"

print("PASS: all stdio-subprocess client-smoke gates satisfied")
print("  readonly tools visible :", len(d["detail"]["readonly_tools_visible"]))
print("  transport              :", d["detail"]["transport"])
print("  frozen_contract_v1     :", d["frozen_contract_v1"])
PYEOF
echo "[4] smoke gates: ALL PASS"

# --- 4. snapshot parent queue AFTER ---------------------------------------
echo ""
echo "=== Queue snapshot AFTER ==="
python3 "$ROOT/AITools/taskctl.py" verify > "$TMPDIR_STATE/verify_after.log" 2>&1 || {
    echo "FAIL: taskctl verify AFTER failed"
    cat "$TMPDIR_STATE/verify_after.log"
    exit 1
}
echo "[5] queue verify AFTER: PASS"

# --- 5. prove parent queue unchanged (diff before/after logs) --------------
if ! diff -q "$TMPDIR_STATE/verify_before.log" "$TMPDIR_STATE/verify_after.log" >/dev/null 2>&1; then
    echo "FAIL: queue verify output changed before vs after"
    diff "$TMPDIR_STATE/verify_before.log" "$TMPDIR_STATE/verify_after.log" || true
    exit 1
fi
echo "[6] queue verify before/after IDENTICAL: PASS (parent queue unchanged)"

# --- 6. produce B118 live-client eval JSON --------------------------------
python3 - "$RESULT" "$EVAL_OUT" "$TMPDIR_STATE" <<'PYEOF'
import json, sys, os
from datetime import datetime, timezone

smoke_result = json.load(open(sys.argv[1]))
eval_out = sys.argv[2]
tmpdir = sys.argv[3]

live_result = {
    "eval_id": "mcp_live_client_smoke_b118_v1",
    "task_id": "DEEPSEEK_TASK_MCP_LIVE_CLIENT_SMOKE_B118_V1",
    "mode": "mcp_live_client_smoke_no_workflow_switch",
    "extends": "mcp_stdio_subprocess_client_smoke_b109_v1",
    "live_client_usable": smoke_result["frozen_contract_v1"],
    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "environment": {
        "GEOAI_TASK_MCP_ALLOW_WRITES": os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES", "0"),
        "GEOAI_REPO": os.environ.get("GEOAI_REPO", ""),
    },
    "smoke": {
        "frozen_contract_v1": smoke_result["frozen_contract_v1"],
        "failing_check": smoke_result["failing_check"],
        "readonly_tool_count": smoke_result["readonly_tool_count"],
        "write_gated_tool_count": smoke_result["write_gated_tool_count"],
        "tools_visible_total": smoke_result["detail"]["total_tools_visible"],
        "transport": smoke_result["detail"]["transport"],
    },
    "queue_integrity": {
        "before_pass": True,
        "after_pass": True,
        "unchanged": True,
    },
    "checks": {
        "allow_writes_zero": os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES", "") == "0",
        "health_readonly_tools_exercised": smoke_result["frozen_contract_v1"],
        "parent_queue_unchanged": True,
        "stdio_transport_live": True,
        "server_no_launch_code": smoke_result["checks"].get("server_no_launch_code", False),
        "taskctl_verify_pass": True,
        "blocked_client": False,
    },
    "authority_flags": {
        "contract_frozen": smoke_result["frozen_contract_v1"],
        "process_launch_authority": False,
        "agent_launch_authority": False,
        "write_gate_enabled": False,
        "runtime_authority": False,
        "default_authority": False,
    },
    "verdict": (
        "LIVE_CLIENT_USABLE"
        if smoke_result["frozen_contract_v1"]
        else "BLOCKED_CLIENT: " + str(smoke_result.get("failing_check", "unknown"))
    ),
}

os.makedirs(os.path.dirname(eval_out), exist_ok=True)
with open(eval_out, "w", encoding="utf-8") as f:
    json.dump(live_result, f, indent=2, ensure_ascii=False, sort_keys=True)
    f.write("\n")

print(f"B118 eval written: {eval_out}")
print(f"  verdict: {live_result['verdict']}")
print(f"  live_client_usable: {live_result['live_client_usable']}")
PYEOF
echo "[7] eval JSON written: $EVAL_OUT"

# --- 7. taskctl verify (final) --------------------------------------------
echo ""
echo "=== Final taskctl verify ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "[8] taskctl verify: PASS"

echo ""
echo "=== B118 LIVE CLIENT SMOKE: ALL CHECKS PASSED ==="
echo "Server is USABLE from local development environment over stdio."
exit 0
