#!/usr/bin/env bash
set -euo pipefail
# B04 partial review self-test: asserts review artifacts exist, parse, and are self-consistent.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EVAL="$ROOT/tools/geoai-task-mcp/eval/mcp_stale_smoke_partial_review_b04_v1.json"
ROWS="$ROOT/tools/geoai-task-mcp/eval/mcp_stale_smoke_partial_review_rows_b04_v1.jsonl"
NEXT="$ROOT/tools/geoai-task-mcp/data/tasking/mcp_stale_smoke_partial_review_next_wave_b04_v1.json"

echo "=== B04 Partial Review Self-Test ==="

python3 - "$EVAL" "$ROWS" "$NEXT" << 'PYEOF'
import json, sys
eval_j = json.load(open(sys.argv[1]))
rows = [json.loads(l) for l in open(sys.argv[2]).readlines() if l.strip()]
nw = json.load(open(sys.argv[3]))

assert eval_j["verdict"] == "RECOVER"
assert eval_j["target_task"] == "DEEPSEEK_TASK_MCP_STALE_SMOKE_INVENTORY_REFRESH_B119_V1"
assert eval_j["verdict_detail"]["force_required"] == False
assert eval_j["summary"]["safe_for_recover_stale"] == True
assert eval_j["smoke_test_results"]["b109_stdio_subprocess_client_smoke"]["verdict"] == "FAIL"
assert eval_j["smoke_test_results"]["b110_stdio_concurrent_client_smoke"]["verdict"] == "FAIL"
assert eval_j["smoke_test_results"]["mvp_contract_audit"]["verdict"] == "FAIL"
assert len(eval_j["artifact_analysis"]["present_files"]) == 3
assert len(eval_j["artifact_analysis"]["missing_files"]) == 2
for pf in eval_j["artifact_analysis"]["present_files"]:
    assert pf["is_b119_output"] == False

assert len(rows) >= 8
assert rows[0]["classification"] == "safe_recover_stale"
assert nw["actions"][0]["action"] == "recover-stale"
assert len(nw["actions"][0]["commands"]) == 1
assert "recover-stale DEEPSEEK_TASK_MCP_STALE_SMOKE_INVENTORY_REFRESH_B119_V1" in nw["actions"][0]["commands"][0]

print("PASS: all B04 review artifacts self-consistent")
PYEOF

echo "ALL CHECKS PASSED"
exit 0
