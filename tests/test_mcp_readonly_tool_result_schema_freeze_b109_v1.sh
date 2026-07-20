#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_readonly_tool_result_schema_freeze_b109_v1.sh
# Freezes the OUTPUT contract of the geoai-task-mcp read-only tools: the
# canonical value->type SKELETON of every read-only tool's structuredContent
# result, snapshot over a REAL MCP ClientSession
# (mcp_readonly_result_schema_freeze.py). Complements B108 (input-schema freeze).
#
# Guarantees asserted here:
#   * all 11 read-only tools visible + each yields a structuredContent skeleton;
#   * skeleton fingerprints byte-stable vs frozen AND deterministic across two
#     independent client sessions (volatile values normalized to type tokens);
#   * FastMCP output-schema fingerprints byte-stable vs frozen (supplementary);
#   * NO WRITES: MCP-owned state dir byte-identical + empty with
#     GEOAI_TASK_MCP_ALLOW_WRITES unset AND =1;
#   * NO PROCESS LAUNCH inside introspection: run_taskctl stubbed, launch
#     tripwire count 0, and launch-free tool modules hold no spawn code.
#
# The ONLY process launch in this test is the final `taskctl verify` parent-
# queue integrity gate, which runs OUTSIDE the in-process introspection harness.
#
# Isolation: harness owns a private mktemp audit/collision state dir; result
# JSON is written to a per-run mktemp path. Parallel-safe; no shared file mutated.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
FREEZE="$MCPROOT/tests/mcp_readonly_result_schema_freeze.py"
SRC_DIR="$MCPROOT/src/geoai_task_mcp"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/geoai_b109_result_freeze.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"

echo "=== MCP Read-only Tool Result-Schema Freeze Test B109 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "TMP=$TMPDIR_STATE"

# --- 0. launch-free tool modules hold no process-launch code ---------------
for mod in server.py cli_adapter_readonly_tool.py cli_adapter_dryrun.py; do
    for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
        if grep -Fq -- "$pat" "$SRC_DIR/$mod"; then
            echo "FAIL: forbidden launch pattern '$pat' found in $mod"
            exit 1
        fi
    done
done
echo "launch-free tool modules: no launch/exec/shell code: OK"

# --- 1. run the freeze harness twice; assert byte-stable + exit 0 ----------
RESULT="$TMPDIR_STATE/result.json"
RESULT2="$TMPDIR_STATE/result2.json"
python3 "$FREEZE" --out "$RESULT" >/dev/null
python3 "$FREEZE" --out "$RESULT2" >/dev/null
if ! cmp -s "$RESULT" "$RESULT2"; then
    echo "FAIL: result JSON not byte-identical across two runs (non-deterministic)"
    exit 1
fi
echo "freeze harness exit 0 + byte-identical across two runs: OK"

# --- 2. assert every gate in the emitted result --------------------------
python3 - "$RESULT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))

assert d["frozen_output_contract_v1"] is True, ("not frozen", d.get("failing_check"))
assert d["failing_check"] is None, d["failing_check"]
assert d["mode"] == "mcp_readonly_output_contract_freeze_no_writes", d["mode"]
assert d["complements"] == "CLAUDE_TASK_MCP_CLIENT_SMOKE_CONTRACT_FREEZE_B108_V1"

checks = d["checks"]
expected = {
    "readonly_tools_visible",
    "result_skeletons_captured",
    "result_skeleton_fingerprints_match_frozen",
    "result_skeleton_deterministic_across_sessions",
    "output_schema_fingerprints_match_frozen",
    "no_write_allow_unset",
    "no_write_allow_set",
    "no_process_launch",
}
assert expected <= set(checks), sorted(expected - set(checks))
for name in expected:
    assert checks[name] is True, f"check failed: {name}"

det = d["detail"]

# 11 read-only tools; a skeleton snapshot for EVERY one
assert d["readonly_tool_count"] == 11, d["readonly_tool_count"]
assert len(det["readonly_tools_visible"]) == 11, det["readonly_tools_visible"]
assert len(det["result_skeletons"]) == 11, sorted(det["result_skeletons"])
assert det["capture_errors"] == [], det["capture_errors"]

# frozen coverage: a frozen fingerprint for every read-only tool, zero drift
assert set(det["frozen_result_skeleton_fingerprints"]) == set(det["readonly_tools_visible"]), \
    "frozen skeleton set != read-only tool set"
assert det["result_skeleton_fingerprint_mismatches"] == [], det["result_skeleton_fingerprint_mismatches"]
assert det["output_schema_fingerprint_mismatches"] == [], det["output_schema_fingerprint_mismatches"]
assert det["current_result_skeleton_fingerprints"] == det["frozen_result_skeleton_fingerprints"], \
    "current != frozen result skeleton fingerprints"

# skeletons are pure key/type structures (values normalized away)
TYPE_TOKENS = {"bool", "int", "number", "str", "null"}
def _is_skel(v):
    if isinstance(v, str):
        return v in TYPE_TOKENS or v.startswith("unknown:")
    if isinstance(v, dict):
        if set(v) == {"__list_of__"}:
            return all(_is_skel(e) for e in v["__list_of__"])
        if set(v) == {"__object__"}:
            return v["__object__"] == "empty"
        return all(_is_skel(x) for x in v.values())
    return False
for tool, skel in det["result_skeletons"].items():
    assert _is_skel(skel), f"skeleton for {tool} contains a raw value: {skel}"

# no-write proof: byte-identical AND empty state dir in BOTH rounds
for rk in ("round_allow_unset", "round_allow_set"):
    r = det[rk]
    assert r["ok"] is True, (rk, "round not ok")
    assert r["state_byte_identical"] is True, (rk, "state not byte-identical")
    assert r["state_empty"] is True, (rk, "state dir not empty")
    assert r["state_after"] == r["state_before"] == [], (rk, r)

# no-launch proof: tripwire never tripped, no spawn code in tool modules
assert det["launch_attempts"] == 0, det["launch_attempts"]
assert det["run_taskctl_stubbed"] is True
assert det["launch_free_module_hits"] == {}, det["launch_free_module_hits"]
assert det["launch_enabled"] is False
assert det["launch_implemented"] is False

# authority flags all stay false
for k, v in d["authority_flags"].items():
    assert v is False, f"authority flag {k} not false"

print("PASS: all read-only result-schema freeze gates satisfied")
print("  read-only tools frozen :", len(det["result_skeletons"]))
print("  skeleton drift         :", det["result_skeleton_fingerprint_mismatches"])
print("  launch_attempts        :", det["launch_attempts"])
PYEOF

# --- 3. parent task queue intact (only process launch, outside harness) ----
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
