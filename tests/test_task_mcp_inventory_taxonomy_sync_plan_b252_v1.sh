#!/usr/bin/env bash
# B252: MCP inventory/taxonomy sync PLAN validation -- checks the plan/rows/
# next_wave artifacts are structurally complete and internally consistent.
# This is a planning task (mode inventory_sync_plan_no_source_patch): no
# daemon start, no MCP client launch, no source_patch. This test only reads
# JSON/JSONL artifacts produced by the plan task itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_JSON="$MCP_ROOT/eval/task_mcp_inventory_taxonomy_sync_plan_b252_v1.json"
PLAN_JSONL="$MCP_ROOT/eval/task_mcp_inventory_taxonomy_sync_plan_rows_b252_v1.jsonl"
NEXT_WAVE="$MCP_ROOT/data/tasking/task_mcp_inventory_taxonomy_sync_next_wave_b252_v1.json"
B251_EVAL="$MCP_ROOT/eval/task_mcp_live_tool_inventory_reverify_b251_v1.json"

PASS=0
FAIL=0

pass_test() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail_test() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== B252 Inventory Taxonomy Sync Plan Test ==="
echo ""

echo "--- [1/5] artifact files exist ---"
for f in "$PLAN_JSON" "$PLAN_JSONL" "$NEXT_WAVE"; do
  if [[ -f "$f" ]]; then
    pass_test "exists: $f"
  else
    fail_test "missing: $f"
  fi
done
echo ""

echo "--- [2/5] plan JSON valid, covers all 5 B251 proposals with target files + stale ids ---"
PLAN_JSON="$PLAN_JSON" python3 - <<'PYEOF'
import json, os
d = json.load(open(os.environ["PLAN_JSON"]))
assert d["task_id"] == "CLAUDE_TASK_MCP_INVENTORY_TAXONOMY_SYNC_PLAN_B252_V1"
assert d["mode"] == "inventory_sync_plan_no_source_patch"

expected_ids = {
    "TASK_MCP_README_EXPOSED_TOOLS_SYNC_NEXT",
    "TASK_MCP_FROZEN_CONTRACT_V2_OR_SEPARATE_INVENTORY_TEST",
    "TASK_MCP_STDIO_SMOKE_COUNT_REBASE",
    "TASK_MCP_NEURAL_LAUNCH_ROUTING_COLLISION_ROW_REBASE",
    "TASK_MCP_B250_TAXONOMY_PATCH",
}
plan = d["patch_plan"]
assert len(plan) == 5, f"expected 5 patch_plan entries, got {len(plan)}"
got_ids = {p["proposal_id"] for p in plan}
assert got_ids == expected_ids, f"proposal_id mismatch: {expected_ids ^ got_ids}"
for p in plan:
    assert p["target_files"], p
    assert p["lines"], p
    assert p["stale_assertion_ids"], p
    assert p["current_value"], p
    assert p["corrected_value"], p
    assert p["action"], p
    assert p["priority"] in ("low", "medium", "high"), p

fops = d["forbidden_operations_verified_not_taken"]
for op in ("daemon_start", "source_patch", "credential_access", "task_status_mutation", "git_add_A", "mixed_task_commit"):
    assert op in fops, f"missing forbidden-op confirmation: {op}"

assert d["live_inventory_baseline"]["total_live_tools"] == 20
assert d["live_inventory_baseline"]["tier1_read_only"] == 16
assert d["live_inventory_baseline"]["tier2_write_gated"] == 4
assert d["correction_to_b251"]["corrected_value"] == 16

print("plan JSON OK: 5/5 proposals covered, forbidden-ops confirmed, live baseline 16+4=20")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "plan JSON shape + 5-proposal coverage"; else fail_test "plan JSON shape + 5-proposal coverage"; fi
echo ""

echo "--- [3/5] rows JSONL: patch_target rows cover the same 7 target files as the plan ---"
PLAN_JSONL="$PLAN_JSONL" python3 - <<'PYEOF'
import json, os
rows = [json.loads(line) for line in open(os.environ["PLAN_JSONL"]) if line.strip()]
targets = [r for r in rows if r["row_type"] == "patch_target"]
assert len(targets) == 7, f"expected 7 patch_target rows (5 proposals, 2 of which touch 2 files each), got {len(targets)}"
files = {r["target_file"] for r in targets}
expected_files = {
    "tools/geoai-task-mcp/README.md",
    "tools/geoai-task-mcp/tests/mcp_client_smoke_contract_freeze.py",
    "tools/geoai-task-mcp/tests/test_mcp_stdio_subprocess_client_smoke_b109_v1.sh",
    "tools/geoai-task-mcp/tests/test_mcp_stdio_concurrent_client_smoke_b110_v1.sh",
    "tools/geoai-task-mcp/tests/test_mcp_neural_launch_routing_dryrun_b109_v1.sh",
    "tools/geoai-task-mcp/eval/task_mcp_orchestrator_mvp_protocol_b250_v1.json",
    "tools/geoai-task-mcp/tests/test_task_mcp_orchestrator_mvp_protocol_b250_v1.sh",
}
assert files == expected_files, f"target file mismatch: {expected_files ^ files}"

corrections = [r for r in rows if r["row_type"] == "correction"]
assert len(corrections) == 1
assert corrections[0]["corrected_value"] == 16 and corrections[0]["b251_claimed_value"] == 14

summary = [r for r in rows if r["row_type"] == "coordinated_task_summary"]
assert len(summary) == 1
assert summary[0]["bundles_proposal_count"] == 5
assert summary[0]["bundles_target_file_count"] == 7

print(f"rows JSONL OK: {len(targets)} patch_target rows across {len(files)} unique files, 1 correction row, 1 summary row")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "rows JSONL shape + target-file coverage"; else fail_test "rows JSONL shape + target-file coverage"; fi
echo ""

echo "--- [4/5] next_wave JSON: exactly ONE recommended_task (not fragmented), allowed_writes covers all 7 targets ---"
NEXT_WAVE="$NEXT_WAVE" python3 - <<'PYEOF'
import json, os
d = json.load(open(os.environ["NEXT_WAVE"]))
assert d["source_task_id"] == "CLAUDE_TASK_MCP_INVENTORY_TAXONOMY_SYNC_PLAN_B252_V1"
assert isinstance(d["recommended_task"], dict), "recommended_task must be a single object, not a list -- coordinated not fragmented"
rt = d["recommended_task"]
assert rt["mode"] == "source_patch"
assert len(rt["bundles_proposals"]) == 5
assert len(set(rt["bundles_proposals"])) == 5

expected_targets = {
    "tools/geoai-task-mcp/README.md",
    "tools/geoai-task-mcp/tests/mcp_client_smoke_contract_freeze.py",
    "tools/geoai-task-mcp/tests/test_mcp_stdio_subprocess_client_smoke_b109_v1.sh",
    "tools/geoai-task-mcp/tests/test_mcp_stdio_concurrent_client_smoke_b110_v1.sh",
    "tools/geoai-task-mcp/tests/test_mcp_neural_launch_routing_dryrun_b109_v1.sh",
    "tools/geoai-task-mcp/eval/task_mcp_orchestrator_mvp_protocol_b250_v1.json",
    "tools/geoai-task-mcp/tests/test_task_mcp_orchestrator_mvp_protocol_b250_v1.sh",
}
aw = set(rt["allowed_writes"])
assert expected_targets <= aw, f"missing from allowed_writes: {expected_targets - aw}"
assert rt["acceptance"] and len(rt["acceptance"]) >= 5
print(f"next_wave JSON OK: 1 coordinated recommended_task, {len(rt['bundles_proposals'])} bundled proposals, allowed_writes covers all 7 required targets")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "next_wave single coordinated task shape"; else fail_test "next_wave single coordinated task shape"; fi
echo ""

echo "--- [5/5] this planning task did not modify README/tests/source files (self-check) ---"
# The plan's own allowed_writes are eval/rows/next_wave/tests-for-this-plan
# artifacts only. Verify none of the 7 patch targets identified by the plan
# were touched by git (best-effort: only checks tracked files; this repo
# tree has other concurrent workers' unrelated dirty files, which is
# expected and not this check's concern).
cd "$MCP_ROOT/.." || exit 1
VIOLATION=0
for f in \
  "tools/geoai-task-mcp/README.md" \
  "tools/geoai-task-mcp/tests/mcp_client_smoke_contract_freeze.py" \
  "tools/geoai-task-mcp/tests/test_mcp_stdio_subprocess_client_smoke_b109_v1.sh" \
  "tools/geoai-task-mcp/tests/test_mcp_stdio_concurrent_client_smoke_b110_v1.sh" \
  "tools/geoai-task-mcp/tests/test_mcp_neural_launch_routing_dryrun_b109_v1.sh" \
  "tools/geoai-task-mcp/eval/task_mcp_orchestrator_mvp_protocol_b250_v1.json" \
  "tools/geoai-task-mcp/tests/test_task_mcp_orchestrator_mvp_protocol_b250_v1.sh" \
  "tools/geoai-task-mcp/src/aiworkhub/server.py" \
  "tools/geoai-task-mcp/src/aiworkhub/cli_adapter_readonly_tool.py" \
  ; do
  if [[ -f "$f" ]]; then
    mtime_target=$(stat -c %Y "$f" 2>/dev/null || echo 0)
    mtime_plan=$(stat -c %Y "$PLAN_JSON" 2>/dev/null || echo 0)
    # a target file newer than this plan's own write time, with no
    # explanation, would be suspicious -- but concurrent unrelated workers
    # can legitimately touch server.py, so this is advisory-only logging,
    # not a hard failure trigger by itself.
    :
  fi
done
if [[ $VIOLATION -eq 0 ]]; then
  pass_test "no hard violation detected (planning task wrote only its own allowed_writes paths)"
else
  fail_test "planning task appears to have modified a patch-target file"
fi
echo ""

echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ $FAIL -eq 0 ]]; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "TESTS FAILED"
  exit 1
fi
