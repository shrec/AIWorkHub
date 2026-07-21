#!/bin/bash
set -e

echo "Testing MCP Project Switch Readiness Gate B119 V1..."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

BUILDER="tools/geoai-task-mcp/scripts/build_mcp_project_switch_readiness_gate_b119_v1.py"
OUT_JSON="tools/geoai-task-mcp/eval/mcp_project_switch_readiness_gate_b119_v1.json"
OUT_ROWS="tools/geoai-task-mcp/eval/mcp_project_switch_readiness_gate_rows_b119_v1.jsonl"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# ── 1. Default run: real evidence files, embedded default queue snapshot ──
echo "1. Default run (real evidence + default queue snapshot)..."
python3 "$BUILDER"

python3 -c "import json; json.load(open('$OUT_JSON'))" || { echo "FAIL: eval json not valid JSON"; exit 1; }
python3 - <<'PYEOF'
import json
with open("tools/geoai-task-mcp/eval/mcp_project_switch_readiness_gate_rows_b119_v1.jsonl") as fh:
    n = 0
    for line in fh:
        line = line.strip()
        if not line:
            continue
        json.loads(line)
        n += 1
assert n == 23, f"expected 23 checkbox rows, got {n}"
PYEOF
echo "Default run outputs are valid JSON/JSONL with 23 checkbox rows."

# ── 2. Current-known-state assertions: not all B119 gates are finished yet ──
echo "2. Testing current-state gate correctness (allowlist/batch_guard/full_wave pending)..."
python3 - <<'PYEOF'
import json
d = json.load(open("tools/geoai-task-mcp/eval/mcp_project_switch_readiness_gate_b119_v1.json"))

assert d["authority_flags"]["workflow_switch"] is False
assert d["authority_flags"]["queue_mutation"] is False
assert d["authority_flags"]["process_launch"] is False

assert d["gates"]["live_client"]["pass"] is True, "live_client gate should pass (B118 smoke evidence)"
assert d["gates"]["dryrun"]["pass"] is True, "dryrun gate should pass (B118 autopickup dryrun evidence)"

# workflow_switch_ready must be False unless ALL 5 named gates pass.
named = d["named_gate_keys"]
all_pass = all(d["gates"][k]["pass"] for k in named)
assert d["workflow_switch_ready"] == all_pass
if not all_pass:
    assert d["workflow_switch_ready"] is False
    assert len(d["blockers"]) > 0, "must record blockers when not ready"

rc = d["readiness_classes"]
assert rc["read_only_ready"] is True, "read-only ready must be true (live client smoke proves it)"
assert rc["launch_disabled_ready"] is True, "launch-disabled contract (B117) proves disabled-state safety"
assert rc["launch_enabled_not_ready"] is True, "launch is NOT implemented; must be flagged not-ready"
# write_gated_ready requires BOTH allowlist enforcement and batch guard finalized.
if not (d["gates"]["allowlist"]["pass"] and d["gates"]["batch_guard"]["pass"]):
    assert rc["write_gated_ready"] is False

assert d["by_phase"]["phase_0"]["total"] == 6
assert d["by_phase"]["phase_1"]["total"] == 5
assert d["by_phase"]["phase_2"]["total"] == 7
assert d["by_phase"]["phase_3"]["total"] == 5
total = sum(d["summary"][k] for k in ("done", "gap", "not_started", "blocked"))
assert total == d["summary"]["total_checkboxes"] == 23
print("Current-state gate assertions OK.")
PYEOF

# ── 3. Decision-logic rigor: synthetic snapshot where ALL gates are finished ──
echo "3. Testing decision logic flips to ready when all 3 in-flight tasks are finished (synthetic)..."
ALL_DONE_SNAPSHOT="$TMPDIR/queue_status_snapshot_all_done.json"
cat > "$ALL_DONE_SNAPSHOT" <<'JSON'
{
  "captured_at": "synthetic-test-all-done",
  "captured_via": "synthetic override for decision-logic test",
  "statuses": {
    "CLAUDE_TASK_MCP_RUNNER_TOPIC_ALLOWLIST_ENFORCEMENT_B119_V1": "finished",
    "DEEPSEEK_TASK_MCP_BATCH_COLLISION_GUARD_B119_V1": "finished",
    "DEEPSEEK_TASK_MCP_FULL_WAVE_DRYRUN_HARNESS_B119_V1": "finished"
  }
}
JSON

ALL_DONE_OUT="$TMPDIR/all_done_gate.json"
ALL_DONE_ROWS="$TMPDIR/all_done_rows.jsonl"
python3 "$BUILDER" --queue-status-snapshot "$ALL_DONE_SNAPSHOT" --out "$ALL_DONE_OUT" --out-rows "$ALL_DONE_ROWS"

python3 - "$ALL_DONE_OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["gates"]["allowlist"]["pass"] is True
assert d["gates"]["batch_guard"]["pass"] is True
assert d["gates"]["full_wave_dryrun"]["pass"] is True
assert d["workflow_switch_ready"] is True, "all 5 gates pass -> workflow_switch_ready must flip True"
assert len(d["blockers"]) <= 2  # only possible remaining blockers: phase_2 gaps + no_parent_corruption
for b in d["blockers"]:
    assert b["id"] in ("phase_2_gaps_open", "p3_no_parent_corruption_not_started")
print("Synthetic all-done snapshot correctly flips workflow_switch_ready=True.")
PYEOF

# ── 4. Synthetic snapshot where only ONE of the three is finished -> still not ready ──
echo "4. Testing partial-completion snapshot stays not-ready..."
PARTIAL_SNAPSHOT="$TMPDIR/queue_status_snapshot_partial.json"
cat > "$PARTIAL_SNAPSHOT" <<'JSON'
{
  "captured_at": "synthetic-test-partial",
  "captured_via": "synthetic override for decision-logic test",
  "statuses": {
    "CLAUDE_TASK_MCP_RUNNER_TOPIC_ALLOWLIST_ENFORCEMENT_B119_V1": "finished",
    "DEEPSEEK_TASK_MCP_BATCH_COLLISION_GUARD_B119_V1": "review",
    "DEEPSEEK_TASK_MCP_FULL_WAVE_DRYRUN_HARNESS_B119_V1": "review"
  }
}
JSON
PARTIAL_OUT="$TMPDIR/partial_gate.json"
PARTIAL_ROWS="$TMPDIR/partial_rows.jsonl"
python3 "$BUILDER" --queue-status-snapshot "$PARTIAL_SNAPSHOT" --out "$PARTIAL_OUT" --out-rows "$PARTIAL_ROWS"
python3 - "$PARTIAL_OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["gates"]["allowlist"]["pass"] is True
assert d["gates"]["batch_guard"]["pass"] is False
assert d["gates"]["full_wave_dryrun"]["pass"] is False
assert d["workflow_switch_ready"] is False
assert len(d["blockers"]) >= 2
print("Partial-completion snapshot correctly stays not-ready.")
PYEOF

# ── 5. Missing-evidence fail-closed check ──
echo "5. Testing fail-closed behavior when finishline rows are missing..."
if python3 "$BUILDER" --finishline-rows "$TMPDIR/does_not_exist.jsonl" --out "$TMPDIR/missing.json" --out-rows "$TMPDIR/missing_rows.jsonl" >"$TMPDIR/missing.out" 2>"$TMPDIR/missing.err"; then
    echo "FAIL: builder should exit nonzero when finishline rows evidence is missing."
    exit 1
fi
grep -q "FATAL" "$TMPDIR/missing.out" || { echo "FAIL: expected FATAL fail-closed message"; exit 1; }
echo "Fail-closed behavior OK."

# ── 6. Determinism: two default runs produce identical gate/summary/blockers ──
echo "6. Testing determinism across repeated default runs..."
RUN1="$TMPDIR/run1.json"
RUN2="$TMPDIR/run2.json"
python3 "$BUILDER" --out "$RUN1" --out-rows "$TMPDIR/run1_rows.jsonl"
python3 "$BUILDER" --out "$RUN2" --out-rows "$TMPDIR/run2_rows.jsonl"
python3 - "$RUN1" "$RUN2" <<'PYEOF'
import json, sys
a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
for key in ("workflow_switch_ready", "gates", "summary", "by_phase", "readiness_classes", "blockers"):
    aa, bb = dict(a[key]) if isinstance(a[key], dict) else a[key], dict(b[key]) if isinstance(b[key], dict) else b[key]
    assert aa == bb, f"non-deterministic field: {key}"
print("Determinism OK across repeated runs (excluding timestamps).")
PYEOF

# ── 7. Restore real default outputs (in case earlier steps overwrote them identically anyway) ──
python3 "$BUILDER" >/dev/null

# ── 8. Parent taskctl verify must still pass (no queue mutation side effect) ──
echo "8. Running taskctl verify (no mutation check)..."
python3 AITools/taskctl.py verify

echo "All tests passed."
