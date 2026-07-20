#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_client_smoke_contract_freeze_b108_v1.sh
# Drives the geoai-task-mcp FastMCP server through a REAL MCP ClientSession
# (mcp_client_smoke_contract_freeze.py) and asserts the read-only tool
# contract v1 is frozen: read-only tools visible over tools/list, input
# schemas byte-stable vs frozen fingerprints, and NO queue/audit writes with
# GEOAI_TASK_MCP_ALLOW_WRITES unset AND =1. Also asserts server.py holds no
# process-launch code. Parent task queue must stay verify-intact.
#
# Isolation: the smoke owns a private mktemp audit state dir; the result JSON
# is written to a per-run mktemp path. Parallel-safe; no shared file mutated.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
SMOKE="$MCPROOT/tests/mcp_client_smoke_contract_freeze.py"
SERVER_SRC="$MCPROOT/src/geoai_task_mcp/server.py"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/geoai_b108_client_smoke.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"

echo "=== MCP Client Smoke Contract Freeze Test B108 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "TMP=$TMPDIR_STATE"

# --- 0. server.py holds no process-launch code (defense in depth) ----------
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$SERVER_SRC"; then
        echo "FAIL: forbidden launch pattern '$pat' found in server.py"
        exit 1
    fi
done
echo "server.py: no launch/exec/shell code: OK"

# --- 1. run the real MCP-client smoke; must exit 0 (frozen_contract_v1) ----
RESULT="$TMPDIR_STATE/result.json"
python3 "$SMOKE" --out "$RESULT" >/dev/null
echo "smoke exit 0 (frozen_contract_v1=true): OK"

# --- 2. assert every gate in the emitted result --------------------------
python3 - "$RESULT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))

assert d["frozen_contract_v1"] is True, ("frozen_contract_v1 not true", d.get("failing_check"))
assert d["failing_check"] is None, d["failing_check"]
assert d["mode"] == "mcp_client_smoke_contract_freeze_no_launch", d["mode"]

checks = d["checks"]
expected = {
    "readonly_tools_visible",
    "write_gated_tools_visible",
    "schema_fingerprints_match_frozen",
    "schema_deterministic_across_sessions",
    "no_write_allow_unset",
    "no_write_allow_set",
    "no_process_launch",
}
assert expected <= set(checks), sorted(expected - set(checks))
for name in expected:
    assert checks[name] is True, f"check failed: {name}"

# read-only tool contract completeness
assert d["readonly_tool_count"] == 11, d["readonly_tool_count"]
assert d["write_gated_tool_count"] == 4, d["write_gated_tool_count"]
assert len(d["detail"]["readonly_tools_visible"]) == 11, d["detail"]["readonly_tools_visible"]

# no-write proof: byte-identical AND empty state dir in BOTH rounds
for rk in ("round_allow_unset", "round_allow_set"):
    r = d["detail"][rk]
    assert r["state_byte_identical"] is True, (rk, "state not byte-identical")
    assert r["state_empty"] is True, (rk, "state dir not empty")
    assert r["queue_verify_intact"] is True, (rk, "queue verify not intact")
    assert r["state_after"] == r["state_before"] == [], (rk, r)

# no launch proof
assert d["detail"]["server_launch_pattern_hits"] == [], d["detail"]["server_launch_pattern_hits"]
assert d["detail"]["launch_enabled"] is False
assert d["detail"]["launch_implemented"] is False

# schema stability: zero fingerprint drift
assert d["detail"]["schema_fingerprint_mismatches"] == [], d["detail"]["schema_fingerprint_mismatches"]

# authority flags all stay false
for k, v in d["authority_flags"].items():
    assert v is False, f"authority flag {k} not false"

print("PASS: all client-smoke contract-freeze gates satisfied")
print("  readonly tools visible :", len(d["detail"]["readonly_tools_visible"]))
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
