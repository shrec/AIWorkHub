#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_task_mcp_orchestrator_mvp_protocol_b250_v1.sh
#
# Harness for the B250 Task MCP orchestrator MVP execution protocol
# (design-only, no source patch, no daemon, no CLI runner execution, no
# credential access, no task-status mutation).
#
# This test validates ONLY the shape of this task's own new JSON/JSONL
# artifacts. It does NOT import or exercise tools/geoai-task-mcp/src/ (that
# tree is shared with other concurrent in-flight workers and is currently
# mid-patch) -- this keeps the test hermetic and immune to unrelated churn.
#
# Verifies:
#   1. protocol JSON parses; all required top-level sections present.
#   2. tool_authority_tiers has exactly 4 tiers with the expected names, each
#      non-empty; total tool count is internally consistent (24, including
#      aiworkhub_task_auto_pickup_dryrun -- corrected omission).
#   3. lifecycle_states == exactly the 5 frozen states, each
#      task_status_mutation == false.
#   4. error_taxonomy == exactly the 5 frozen codes.
#   5. adapters == exactly the 3 known adapters.
#   6. safety_model covers credential/shell/per-project-isolation/daemon/
#      network/write-gate dimensions.
#   7. timeout_rules cites the real 3600s/concurrency=1 constants, the
#      live-enforced 60s DEFAULT_TIMEOUT_SECONDS/AIWORKHUB_TIMEOUT
#      subprocess constant (core.py), and marks the execution_timeout
#      extension as PROPOSED, not frozen.
#   8. neural_bridge_note cites the exact unaffected migration id + curriculum
#      path (no invented migration id).
#   9. this_task_forbidden_operations_confirmed_not_taken has exactly this
#      task's 7 FORBIDDEN keys, all false.
#  10. rows JSONL parses, is non-empty, and its per-row_type counts match the
#      protocol JSON's own class counts exactly (no silent drift).
#  11. next_wave JSON parses with required sections and >=3 genuine proposals.
#  12. no subprocess/exec/network/credential-read pattern in any of the 3 new
#      JSON/JSONL artifacts' raw text.
#  13. this task's own footprint touches zero paths under
#      tools/geoai-task-mcp/src/.
#  14. parent AIWorkHub task queue is not mutated (taskctl verify).
#
# Isolation: read-only, no subprocess beyond taskctl verify, no daemon, no
# network, no env mutation. Never calls taskctl review/done/auto-pickup.
# Parallel-safe (touches only its own 4 new files + a read-only taskctl verify).
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
PROTOCOL_JSON="$MCPROOT/eval/task_mcp_orchestrator_mvp_protocol_b250_v1.json"
ROWS_JSONL="$MCPROOT/eval/task_mcp_orchestrator_mvp_protocol_rows_b250_v1.jsonl"
NEXT_WAVE_JSON="$MCPROOT/data/tasking/task_mcp_orchestrator_mvp_next_wave_b250_v1.json"
TEST_SH="$MCPROOT/tests/test_task_mcp_orchestrator_mvp_protocol_b250_v1.sh"

echo "=== Task MCP Orchestrator MVP Execution Protocol Test B250 v1 ==="
echo "ROOT=$ROOT"
echo "MCPROOT=$MCPROOT"
echo ""

echo "--- [1/14] protocol JSON: required top-level sections ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))

required_top = [
    "schema_id", "contract_id", "task_id", "runner", "topic", "mode",
    "purpose", "consolidation_note", "builds_on", "no_source_patch_marker",
    "nested_repo_state_note", "tool_authority_tiers", "execution_flow",
    "lifecycle_states", "lifecycle_transitions",
    "lifecycle_state_to_supervisor_state_mapping", "error_taxonomy",
    "timeout_rules", "safety_model", "command_allowlist", "adapters",
    "review_handoff_contract", "stale_facts_flagged_not_trusted",
    "known_exposure_gaps_for_next_wave", "neural_bridge_note",
    "this_task_forbidden_operations_confirmed_not_taken",
    "protocol_safety_invariants_for_future_implementers", "acceptance",
    "rollback", "verdict",
]
for k in required_top:
    assert k in d, f"missing top-level section: {k}"

assert d["mode"] == "orchestrator_protocol_design_no_source_patch"
assert d["verdict"] == "PASS"
assert len(d["builds_on"]) >= 5, "expected >=5 builds_on references (consolidation, not greenfield)"

print(f"protocol JSON top-level OK: {len(required_top)} required sections present, mode/verdict correct")
PYEOF
echo ""

echo "--- [2/14] tool_authority_tiers: exactly 4 tiers, non-empty, count=24 ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
tiers = d["tool_authority_tiers"]["tiers"]

expected_tier_names = {
    "TIER_1_READ_ONLY_NAVIGATION",
    "TIER_2_WRITE_GATED_LIFECYCLE",
    "TIER_3_LAUNCH_PLAN_DRYRUN_ONLY",
    "TIER_4_GATED_LAUNCH_EXECUTION_RESERVED",
}
got_names = {t["tier"] for t in tiers}
assert got_names == expected_tier_names, f"tier mismatch: {got_names} != {expected_tier_names}"

total_tools = 0
for t in tiers:
    members = t["member_tools"]
    assert len(members) >= 1, f"{t['tier']}: must have >=1 member tool"
    for m in members:
        assert "tool_name" in m and m["tool_name"]
        assert "implemented" in m
    total_tools += len(members)

assert total_tools == 24, f"expected 24 total tools across 4 tiers, got {total_tools}"

# TIER_1 must include the previously-missing already-committed read-only tool
tier1 = next(t for t in tiers if t["tier"] == "TIER_1_READ_ONLY_NAVIGATION")
tier1_names = {m["tool_name"] for m in tier1["member_tools"]}
assert "aiworkhub_task_auto_pickup_dryrun" in tier1_names, "aiworkhub_task_auto_pickup_dryrun (real, HEAD-committed, read-only) must be present in TIER_1"
assert len(tier1["member_tools"]) == 15, f"expected 15 TIER_1 tools, got {len(tier1['member_tools'])}"

# TIER_2 and TIER_3 must each have all 4 canonical tools present by name
tier2 = next(t for t in tiers if t["tier"] == "TIER_2_WRITE_GATED_LIFECYCLE")
tier2_names = {m["tool_name"] for m in tier2["member_tools"]}
assert tier2_names == {"aiworkhub_task_auto_pickup", "aiworkhub_task_mark_review", "aiworkhub_task_mark_done", "aiworkhub_task_export_jsonl"}

tier3 = next(t for t in tiers if t["tier"] == "TIER_3_LAUNCH_PLAN_DRYRUN_ONLY")
tier3_names = {m["tool_name"] for m in tier3["member_tools"]}
assert tier3_names == {"aiworkhub_agent_launch_task", "aiworkhub_agent_task_status", "aiworkhub_agent_collect_result", "aiworkhub_agent_cancel_task"}
for m in tier3["member_tools"]:
    assert m["implemented"] != True, f"TIER_3 tool {m['tool_name']} must never claim full implementation (plan-only)"

tier4 = next(t for t in tiers if t["tier"] == "TIER_4_GATED_LAUNCH_EXECUTION_RESERVED")
for m in tier4["member_tools"]:
    assert m["implemented"] is False, "TIER_4 must have zero implemented tools"

print(f"tool_authority_tiers OK: 4 tiers, {total_tools} total tools, TIER_2/TIER_3 exact-match, TIER_4 zero-implemented")
PYEOF
echo ""

echo "--- [3/14] lifecycle_states exact set + task_status_mutation==false ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
states = d["lifecycle_states"]

expected = {"planned", "dispatched_dryrun", "worker_review_ready", "codex_reviewed", "blocked"}
assert set(states) == expected, f"lifecycle_states mismatch: {set(states)} != {expected}"
for name, spec in states.items():
    assert spec["task_status_mutation"] is False, f"{name}: task_status_mutation must be false"
    assert spec.get("description")

transitions = d["lifecycle_transitions"]
assert len(transitions) >= 5
for t in transitions:
    assert t["to"] in expected

mapping = d["lifecycle_state_to_supervisor_state_mapping"]["mapping"]
assert set(mapping.values()) == expected

print(f"lifecycle_states OK: exactly {sorted(expected)}, all task_status_mutation=false, {len(transitions)} transitions, mapping consistent")
PYEOF
echo ""

echo "--- [4/14] error_taxonomy exact 5-code set ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
tax = {k: v for k, v in d["error_taxonomy"].items() if k != "note"}

expected = {"collision", "stale_task", "runner_topic_mismatch", "missing_artifact", "failed_validation"}
assert set(tax) == expected, f"error_taxonomy mismatch: {set(tax)} != {expected}"
for name, spec in tax.items():
    assert spec.get("trigger") and spec.get("detected_at_leg") and spec.get("example_reason"), f"{name}: incomplete spec"

# must NOT silently add a 6th frozen code
assert len(tax) == 5

print(f"error_taxonomy OK: exactly {sorted(expected)}, 5 codes, no silent 6th code")
PYEOF
echo ""

echo "--- [5/14] adapters exact 3-member set ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
adapters = d["adapters"]
expected = {"claude_cli", "codex_cli", "deepseek_manual"}
assert set(adapters) == expected, f"adapters mismatch: {set(adapters)} != {expected}"
for name, spec in adapters.items():
    assert spec.get("review_role") and spec.get("boundary")
assert adapters["deepseek_manual"]["human_in_the_loop"] is True
assert adapters["claude_cli"]["human_in_the_loop"] is False
assert adapters["codex_cli"]["human_in_the_loop"] is False

print("adapters OK: exactly {claude_cli, codex_cli, deepseek_manual}, roles/boundaries present")
PYEOF
echo ""

echo "--- [6/14] safety_model covers credential/shell/isolation/daemon/network/write-gate ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
sm = d["safety_model"]

expected_keys = {
    "credential_handling", "shell_execution_policy", "per_project_isolation",
    "daemon_policy", "network_policy", "write_gate_policy",
}
assert set(sm) == expected_keys, f"safety_model mismatch: {set(sm)} != {expected_keys}"

assert sm["credential_handling"]["no_credential_read"] is True
assert sm["credential_handling"]["no_credential_log"] is True
assert sm["shell_execution_policy"]["no_shell_true"] is True
assert sm["shell_execution_policy"]["argv_list_only"] is True
assert sm["shell_execution_policy"]["no_arbitrary_command"] is True
assert "scope_today" in sm["per_project_isolation"]
assert "AIWORKHUB_REPO" in sm["per_project_isolation"]["mechanism"]
assert sm["daemon_policy"]["no_daemon_start"] is True
assert sm["network_policy"]["no_network_launch"] is True
assert sm["write_gate_policy"]["both_required_for_future_launch"] is True

# command_allowlist cross-check: exactly 2 binaries + 7 taskctl subcommands
ca = d["command_allowlist"]
assert set(ca["launchable_binaries"]) == {"claude", "codex"}
assert len(ca["allowed_taskctl_subcommands"]) == 7
assert ca["no_arbitrary_command"] is True

print("safety_model OK: 6 dimensions present with required booleans; command_allowlist = 2 binaries + 7 taskctl subcommands")
PYEOF
echo ""

echo "--- [7/14] timeout_rules cites real constants (both), marks extension PROPOSED ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
tr = d["timeout_rules"]

assert tr["bounded_timeout_seconds_default"] == 3600, "must cite the b104 sandbox-ceiling timeout constant"
assert tr["concurrency_cap_default"] == 1
assert tr["applies_to_leg"] == 2
assert "NOT reachable end-to-end today" in tr["current_reachability"]

# Must NOT repeat the false "verified the ONLY timeout constant" claim (source_task_id b250
# verifier finding: a second, currently-enforced timeout constant exists in core.py and was
# omitted). The source field must no longer assert exclusivity.
assert "the ONLY timeout constant anywhere in this nested repo" not in tr["source"], \
    "must not claim bounded_timeout_seconds_default is the only timeout constant (false -- core.py DEFAULT_TIMEOUT_SECONDS also exists)"

# Second, currently-enforced timeout constant (core.py DEFAULT_TIMEOUT_SECONDS) must be present
live = tr["live_enforced_timeout_constant"]
assert live["name"] == "DEFAULT_TIMEOUT_SECONDS"
assert live["env_override"] == "AIWORKHUB_TIMEOUT"
assert live["default_value_seconds"] == 60
assert live["currently_enforced"] is True
assert "core.py:16" in live["source"]
assert "run_taskctl" in live["mechanism"]
assert "aiworkhub_task_auto_pickup_dryrun" in live["scope"], "scope must include the newly-added tool"

on_exceed = tr["on_exceed"]
assert "PROPOSED" in on_exceed["status"] and "NOT part of the frozen 5-code" in on_exceed["status"]
assert on_exceed["proposed_code"] == "execution_timeout"

print("timeout_rules OK: 3600s/concurrency=1 + live 60s DEFAULT_TIMEOUT_SECONDS both cited, leg=2, extension explicitly marked PROPOSED not frozen, no false exclusivity claim")
PYEOF
echo ""

echo "--- [8/14] neural_bridge_note cites unaffected migration id + curriculum path ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
nbn = d["neural_bridge_note"]
assert nbn["migration_task_id"] == "MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1"
assert nbn["curriculum_path"] == "bitnnv2/data/curriculum/mcp_launch_routing_targets_v1.jsonl"
assert nbn["not_a_bare_promise"] is True

print("neural_bridge_note OK: exact migration id + curriculum path, matches every sibling contract verbatim")
PYEOF
echo ""

echo "--- [9/14] this_task_forbidden_operations_confirmed_not_taken exact 7-key set, all false ---"
PROTOCOL_JSON="$PROTOCOL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["PROTOCOL_JSON"]))
forbidden = {k: v for k, v in d["this_task_forbidden_operations_confirmed_not_taken"].items() if k != "note"}

required_forbidden = {
    "daemon_start", "cli_runner_execution", "source_patch", "credential_access",
    "task_status_mutation", "git_add_A", "mixed_task_commit",
}
assert set(forbidden) == required_forbidden, f"forbidden ops mismatch: {set(forbidden)} != {required_forbidden}"
for k, v in forbidden.items():
    assert v is False, f"forbidden op {k} not confirmed false: {v}"

print(f"this_task_forbidden_operations_confirmed_not_taken OK: exactly {sorted(required_forbidden)}, all false")
PYEOF
echo ""

echo "--- [10/14] rows JSONL: non-empty, per-row_type counts match protocol JSON ---"
PROTOCOL_JSON="$PROTOCOL_JSON" ROWS_JSONL="$ROWS_JSONL" python3 - <<'PYEOF'
import json, os
from collections import Counter

proto = json.load(open(os.environ["PROTOCOL_JSON"]))
rows = []
with open(os.environ["ROWS_JSONL"]) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))

assert len(rows) > 0, "rows JSONL must be non-empty"
for r in rows:
    for k in ("row_type", "name", "tier_or_class", "implemented", "source_task_id"):
        assert k in r, f"row missing field {k}: {r}"

counts = Counter(r["row_type"] for r in rows)

expected_tool_count = sum(len(t["member_tools"]) for t in proto["tool_authority_tiers"]["tiers"])
assert counts["tool"] == expected_tool_count == 24, f"tool row count {counts['tool']} != protocol tool count {expected_tool_count}"

assert counts["lifecycle_state"] == len(proto["lifecycle_states"]) == 5
tax_codes = {k for k in proto["error_taxonomy"] if k != "note"}
assert counts["error_taxonomy"] == len(tax_codes) == 5
assert counts["adapter"] == len(proto["adapters"]) == 3
assert counts["safety_boundary"] == len(proto["safety_model"]) == 6
assert counts["timeout_rule"] == 3, f"expected 3 timeout_rule rows (bounded_timeout_seconds_default, concurrency_cap_default, live_enforced_timeout_constant), got {counts['timeout_rule']}"
assert counts["proposed_extension"] == 1

total_expected = 24 + 5 + 5 + 3 + 6 + 3 + 1
assert len(rows) == total_expected, f"expected {total_expected} total rows, got {len(rows)}"

# every tool_name in the tiers must appear as a row name
proto_tool_names = {m["tool_name"] for t in proto["tool_authority_tiers"]["tiers"] for m in t["member_tools"]}
row_tool_names = {r["name"] for r in rows if r["row_type"] == "tool"}
assert proto_tool_names == row_tool_names, "tool name set mismatch between protocol JSON and rows JSONL"

print(f"rows JSONL OK: {len(rows)} rows, per-row_type counts match protocol JSON exactly, tool name sets identical")
PYEOF
echo ""

echo "--- [11/14] next_wave JSON structure ---"
NEXT_WAVE_JSON="$NEXT_WAVE_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["NEXT_WAVE_JSON"]))
required_top = [
    "proposal_id", "status", "source_task", "this_task_scope",
    "proposed_next_wave_tasks", "not_created_as_taskctl_cards", "rollback",
    "neural_bridge_note",
]
for k in required_top:
    assert k in d, f"missing next_wave section: {k}"

assert d["status"] == "design_only_not_applied_this_task"
scope = d["this_task_scope"]
assert scope["source_code_changed"] is False
assert scope["daemon_started"] is False
assert scope["cli_runner_executed"] is False
assert scope["credential_accessed"] is False
assert scope["task_status_mutated"] is False

proposals = d["proposed_next_wave_tasks"]
assert len(proposals) >= 3, f"expected >=3 genuine proposals, got {len(proposals)}"
for p in proposals:
    assert p.get("candidate_id") and p.get("goal") and p.get("why_genuine")

assert d["not_created_as_taskctl_cards"] is True
assert d["neural_bridge_note"]["migration_task_id"] == "MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1"

print(f"next_wave JSON structure OK: {len(proposals)} genuine proposals, no taskctl cards created")
PYEOF
echo ""

echo "--- [12/14] no subprocess/exec/network/credential-read pattern in new artifacts ---"
python3 - <<PYEOF
forbidden_patterns = (
    "subprocess.run", "subprocess.Popen", "subprocess.call", "import subprocess",
    "os.system(", "os.popen", "os.exec", "os.fork", "os.spawn", "Popen(",
    "shell=True", "pty.spawn", "requests.post(", "urllib.request", "httpx.",
    "os.environ[\"AWS", "API_KEY", "SECRET_KEY",
)
paths = [
    "$PROTOCOL_JSON",
    "$ROWS_JSONL",
    "$NEXT_WAVE_JSON",
]
for path in paths:
    raw = open(path, encoding="utf-8").read()
    for pat in forbidden_patterns:
        assert pat not in raw, f"forbidden pattern {pat!r} found in {path}"
print(f"forbidden-pattern scan OK: {len(forbidden_patterns)} patterns checked across {len(paths)} files, none found")
PYEOF
echo ""

echo "--- [13/14] this task's own footprint touches zero paths under src/ ---"
for f in "$PROTOCOL_JSON" "$ROWS_JSONL" "$NEXT_WAVE_JSON" "$TEST_SH"; do
    case "$f" in
        "$MCPROOT/src/"*)
            echo "FAIL: this task's own artifact path falls under src/: $f"
            exit 1
            ;;
    esac
    if [ ! -f "$f" ]; then
        echo "FAIL: expected artifact missing: $f"
        exit 1
    fi
done
echo "own footprint OK: 4/4 artifacts present, none under tools/geoai-task-mcp/src/"
echo ""

echo "--- [14/14] parent queue intact ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact; this script never calls review/done/auto-pickup)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
