#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_unified_contract_freeze_gate_b110_v1.sh
# CLAUDE_TASK_MCP_UNIFIED_CONTRACT_FREEZE_GATE_B110_V1
#
# Composes the two independent MCP contract freezes into ONE gate:
#   * B108 inputSchema freeze   (mcp_client_smoke_contract_freeze.py)
#   * B109 output-schema + result-skeleton freeze
#         (mcp_readonly_result_schema_freeze.py)
# It runs BOTH freeze harnesses FRESH into mktemp result files, then the pure
# aggregator (mcp_unified_contract_freeze_gate.py) binds every input, output-
# schema and result-skeleton fingerprint per read-only tool (plus write-gated
# input schemas) against the single frozen manifest and fails on ANY drift.
#
# Proves the gate has TEETH: after the positive pass, four NEGATIVE runs mutate
# an input fp, an output-schema fp, a result-skeleton fp, and the manifest
# self-fingerprint, and assert the gate exits non-zero each time.
#
# Read-only; launches no agent/model; GEOAI_TASK_MCP_ALLOW_WRITES irrelevant.
# Isolation: all artifacts in a private mktemp dir; no shared file mutated.
# Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
GATE="$MCPROOT/tests/mcp_unified_contract_freeze_gate.py"
B108="$MCPROOT/tests/mcp_client_smoke_contract_freeze.py"
B109="$MCPROOT/tests/mcp_readonly_result_schema_freeze.py"
MANIFEST="$MCPROOT/eval/mcp_unified_contract_freeze_manifest_b110_v1.json"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/geoai_b110_unified_gate.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"

echo "=== MCP Unified Contract Freeze Gate Test B110 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "TMP=$TMP"

# --- 0. gate script holds no launch/exec/shell code (defense in depth) ------
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$GATE"; then
        echo "FAIL: forbidden launch pattern '$pat' found in gate script"
        exit 1
    fi
done
echo "gate script: no launch/exec/shell code: OK"

# --- 1. run BOTH freeze harnesses FRESH ------------------------------------
R108="$TMP/b108.json"
R109="$TMP/b109.json"
python3 "$B108" --out "$R108" >/dev/null
python3 "$B109" --out "$R109" >/dev/null
echo "both freeze harnesses ran fresh: OK"

# --- 2. POSITIVE: unified gate must pass (exit 0, zero drift) ----------------
VERDICT="$TMP/verdict.json"
python3 "$GATE" --manifest "$MANIFEST" --b108 "$R108" --b109 "$R109" --out "$VERDICT"
echo "unified gate positive pass: OK"

python3 - "$VERDICT" "$MANIFEST" <<'PYEOF'
import json, sys
v = json.load(open(sys.argv[1]))
m = json.load(open(sys.argv[2]))
assert v["ok"] is True, v.get("failing_check")
assert v["frozen_unified_contract_v1"] is True
assert v["drift_count"] == 0, v["drift"]
assert v["failing_check"] is None
assert v["readonly_tool_count"] == 11, v["readonly_tool_count"]
assert v["write_gated_tool_count"] == 4, v["write_gated_tool_count"]
assert v["manifest_fingerprint"] == v["manifest_fingerprint_recomputed"], "manifest self-fp drift"
for name, val in v["checks"].items():
    assert val is True, f"gate sub-check failed: {name}"
for k, val in v["authority_flags"].items():
    assert val is False, f"authority flag {k} not false"
# manifest binds all three families for every read-only tool
assert len(m["per_readonly_tool"]) == 11
for tool, b in m["per_readonly_tool"].items():
    assert set(b) == {"input_schema_fp", "output_schema_fp", "result_skeleton_fp"}, (tool, b)
assert len(m["write_gated_input_schema_fp"]) == 4
print("PASS: unified gate binds B108 input + B109 output/skeleton, zero drift")
print("  readonly tools bound :", v["readonly_tool_count"])
print("  manifest fingerprint :", v["manifest_fingerprint"][:16], "...")
PYEOF

# --- 3. NEGATIVE: gate must FAIL on any drift -------------------------------
# helper: mutate one json field with a small python edit, run gate, expect fail
mutate_and_expect_fail () {
    local label="$1"; local target="$2"; local py="$3"; local expect_check="$4"
    local m="$TMP/mut_${label}.json"
    cp "$target" "$m"
    python3 - "$m" <<PYEOF
import json, sys
p = sys.argv[1]
d = json.load(open(p))
$py
json.dump(d, open(p, "w"))
PYEOF
    local mv="$TMP/verdict_${label}.json"
    local rc=0
    if [ "$target" = "$R108" ]; then
        python3 "$GATE" --manifest "$MANIFEST" --b108 "$m" --b109 "$R109" --out "$mv" || rc=$?
    elif [ "$target" = "$R109" ]; then
        python3 "$GATE" --manifest "$MANIFEST" --b108 "$R108" --b109 "$m" --out "$mv" || rc=$?
    else
        python3 "$GATE" --manifest "$m" --b108 "$R108" --b109 "$R109" --out "$mv" || rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
        echo "FAIL[$label]: gate passed on injected drift (expected non-zero exit)"
        exit 1
    fi
    python3 - "$mv" "$expect_check" <<'PYEOF'
import json, sys
v = json.load(open(sys.argv[1]))
want = sys.argv[2]
assert v["ok"] is False, "verdict.ok should be False on drift"
assert v["drift_count"] >= 1, v
checks = {d["check"] for d in v["drift"]}
assert want in checks, (want, sorted(checks))
PYEOF
    echo "negative[$label]: gate correctly FAILED (drift=$expect_check, exit $rc): OK"
}

# 3a. input-schema drift on a read-only tool
mutate_and_expect_fail "input_ro" "$R108" \
    'd["detail"]["frozen_schema_fingerprints"]["geoai_task_list"] = "deadbeef"' \
    "input_schema_drift"

# 3b. write-gated input-schema drift
mutate_and_expect_fail "input_wg" "$R108" \
    'd["detail"]["frozen_schema_fingerprints"]["geoai_task_auto_pickup"] = "deadbeef"' \
    "write_gated_input_drift"

# 3c. output-schema drift on a read-only tool
mutate_and_expect_fail "output_ro" "$R109" \
    'd["detail"]["frozen_output_schema_fingerprints"]["geoai_task_show"] = "deadbeef"' \
    "output_schema_drift"

# 3d. result-skeleton drift on a read-only tool
mutate_and_expect_fail "skel_ro" "$R109" \
    'd["detail"]["frozen_result_skeleton_fingerprints"]["geoai_task_health"] = "deadbeef"' \
    "result_skeleton_drift"

# 3e. manifest tamper (self-fingerprint no longer matches binding)
mutate_and_expect_fail "manifest_tamper" "$MANIFEST" \
    'd["per_readonly_tool"]["geoai_task_health"]["input_schema_fp"] = "deadbeef"' \
    "manifest_fingerprint"

echo "all negative drift injections correctly rejected: OK"

# --- 4. parent task queue intact -------------------------------------------
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
